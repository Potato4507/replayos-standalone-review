from __future__ import annotations

import io
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import math
import re
import time
import warnings
import zlib
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import carball
import duckdb
import numpy as np
import pandas as pd
from carball.analysis.analysis_manager import AnalysisManager
from carball.json_parser.game import Game
from google.protobuf.json_format import MessageToDict

from .car_meta import car_profile
from .config import PROJECT_ROOT, get_settings
from .db import database_connection, rows_to_dicts


PARSER_VERSION = "carball60-v6-native-viewer"
PARSED_PAYLOAD_VERSION = 5
try:
    PARSER_BACKEND_VERSION = package_version("sprocket-boxcars-py")
except PackageNotFoundError:
    PARSER_BACKEND_VERSION = "unknown"
PLAYER_IGNORE = {"G8 REFEREE", "OBS_MAP"}
PLACEHOLDER_TEAM_KEYS = {"blue", "orange", "blue side", "orange side"}
BOOST_BURN_PER_SECOND = 33.3
BOOST_FULL_VALUE = 100.0
BOOST_SMALL_PAD_VALUE = 12.0
SMALL_PAD_COOLDOWN_SECONDS = 4.0
LARGE_PAD_COOLDOWN_SECONDS = 10.0
SMALL_PAD_RADIUS = 230.0
LARGE_PAD_RADIUS = 280.0
BOOST_PAD_LAYOUT = [
    {"pad_id": "small-0", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 1792.0, "y": 4184.0},
    {"pad_id": "small-1", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": -1792.0, "y": 4184.0},
    {"pad_id": "small-2", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 1792.0, "y": -4184.0},
    {"pad_id": "small-3", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": -1792.0, "y": -4184.0},
    {"pad_id": "small-4", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 940.0, "y": 3308.0},
    {"pad_id": "small-5", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": -940.0, "y": 3308.0},
    {"pad_id": "small-6", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 940.0, "y": -3308.0},
    {"pad_id": "small-7", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": -940.0, "y": -3308.0},
    {"pad_id": "small-8", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 1788.0, "y": 2300.0},
    {"pad_id": "small-9", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": -1788.0, "y": 2300.0},
    {"pad_id": "small-10", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 1788.0, "y": -2300.0},
    {"pad_id": "small-11", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": -1788.0, "y": -2300.0},
    {"pad_id": "small-12", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 2048.0, "y": 1036.0},
    {"pad_id": "small-13", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": -2048.0, "y": 1036.0},
    {"pad_id": "small-14", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 2048.0, "y": -1036.0},
    {"pad_id": "small-15", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": -2048.0, "y": -1036.0},
    {"pad_id": "small-16", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 3584.0, "y": 2484.0},
    {"pad_id": "small-17", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": -3584.0, "y": 2484.0},
    {"pad_id": "small-18", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 3584.0, "y": -2484.0},
    {"pad_id": "small-19", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": -3584.0, "y": -2484.0},
    {"pad_id": "small-20", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 0.0, "y": 4240.0},
    {"pad_id": "small-21", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 0.0, "y": -4240.0},
    {"pad_id": "small-22", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 0.0, "y": 2816.0},
    {"pad_id": "small-23", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 0.0, "y": -2816.0},
    {"pad_id": "small-24", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 0.0, "y": 1024.0},
    {"pad_id": "small-25", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 0.0, "y": -1024.0},
    {"pad_id": "small-26", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": 1024.0, "y": 0.0},
    {"pad_id": "small-27", "full_boost": False, "cooldown": SMALL_PAD_COOLDOWN_SECONDS, "radius": SMALL_PAD_RADIUS, "x": -1024.0, "y": 0.0},
    {"pad_id": "large-0", "full_boost": True, "cooldown": LARGE_PAD_COOLDOWN_SECONDS, "radius": LARGE_PAD_RADIUS, "x": 3072.0, "y": 4096.0},
    {"pad_id": "large-1", "full_boost": True, "cooldown": LARGE_PAD_COOLDOWN_SECONDS, "radius": LARGE_PAD_RADIUS, "x": -3072.0, "y": 4096.0},
    {"pad_id": "large-2", "full_boost": True, "cooldown": LARGE_PAD_COOLDOWN_SECONDS, "radius": LARGE_PAD_RADIUS, "x": 3072.0, "y": -4096.0},
    {"pad_id": "large-3", "full_boost": True, "cooldown": LARGE_PAD_COOLDOWN_SECONDS, "radius": LARGE_PAD_RADIUS, "x": -3072.0, "y": -4096.0},
    {"pad_id": "large-4", "full_boost": True, "cooldown": LARGE_PAD_COOLDOWN_SECONDS, "radius": LARGE_PAD_RADIUS, "x": 3584.0, "y": 0.0},
    {"pad_id": "large-5", "full_boost": True, "cooldown": LARGE_PAD_COOLDOWN_SECONDS, "radius": LARGE_PAD_RADIUS, "x": -3584.0, "y": 0.0},
]


class ReplayParseError(RuntimeError):
    """Raised when a replay cannot be parsed into live telemetry."""


def ensure_carball_schema(con: duckdb.DuckDBPyConnection) -> None:
    required_tables = {
        "replay_parsed_status",
        "replay_parsed_frames",
        "replay_parsed_events",
        "local_replay_index",
    }
    if all(_table_exists(con, table_name) for table_name in required_tables):
        return
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS replay_parsed_status (
                replay_id VARCHAR PRIMARY KEY,
                local_file_path VARCHAR,
                parser_name VARCHAR,
                parser_version VARCHAR,
                source_hz BIGINT,
                target_hz BIGINT,
                frame_count BIGINT,
                duration_seconds DOUBLE,
                blue_team_name VARCHAR,
                orange_team_name VARCHAR,
                blue_goals BIGINT,
                orange_goals BIGINT,
                file_size BIGINT,
                file_mtime DOUBLE,
                parsed_at TIMESTAMP,
                parse_seconds DOUBLE,
                status VARCHAR,
                error VARCHAR,
                last_accessed_at TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS replay_parsed_frames (
                replay_id VARCHAR PRIMARY KEY,
                payload_zlib BLOB,
                players_json VARCHAR,
                bounds_json VARCHAR,
                summary_json VARCHAR,
                created_at TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS replay_parsed_events (
                replay_id VARCHAR,
                event_id BIGINT,
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
                meta VARCHAR,
                PRIMARY KEY (replay_id, event_id)
            )
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_replay_parsed_status_last_accessed
            ON replay_parsed_status (last_accessed_at)
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_replay_parsed_events_replay_t
            ON replay_parsed_events (replay_id, t)
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS local_replay_index (
                replay_id VARCHAR PRIMARY KEY,
                local_file_path VARCHAR,
                file_size BIGINT,
                file_mtime DOUBLE,
                discovered_at TIMESTAMP,
                last_seen_at TIMESTAMP,
                in_warehouse BOOLEAN
            )
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_local_replay_index_last_seen_at
            ON local_replay_index (last_seen_at)
            """
        )
    except duckdb.Error as exc:
        if "read-only mode" in str(exc).lower():
            return
        raise


def resolve_local_replay_path(replay_id: str, local_file_path: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if local_file_path:
        candidates.append(Path(local_file_path))
    settings = get_settings()
    candidates.extend(
        [
            settings.replay_download_dir / f"{replay_id}.replay",
            settings.replay_download_dir.parent / f"{replay_id}.replay",
            PROJECT_ROOT / "replays" / f"{replay_id}.replay",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Replay file not found for {replay_id}")


def refresh_local_replay_index(
    *,
    serving_db: Path | None = None,
    replay_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    serving_db = Path(serving_db or settings.serving_db)
    roots = _replay_roots(replay_roots)
    discovered_at = datetime.now(timezone.utc)
    warehouse_ids: set[str] = set()

    con = duckdb.connect(str(serving_db))
    try:
        ensure_carball_schema(con)
        if _table_exists(con, "replays"):
            warehouse_ids.update(
                row[0]
                for row in con.execute("SELECT replay_id FROM replays").fetchall()
                if row and row[0]
            )
        if _table_exists(con, "remote_replays"):
            warehouse_ids.update(
                row[0]
                for row in con.execute("SELECT replay_id FROM remote_replays").fetchall()
                if row and row[0]
            )

        rows_by_replay: dict[str, list[Any]] = {}
        scanned = 0
        for replay_path in _iter_replay_files(roots):
            scanned += 1
            replay_id = replay_path.stem
            if not replay_id:
                continue
            stat = replay_path.stat()
            candidate = [
                replay_id,
                str(replay_path),
                int(stat.st_size),
                float(stat.st_mtime),
                discovered_at,
                discovered_at,
                replay_id in warehouse_ids,
            ]
            current = rows_by_replay.get(replay_id)
            if current is None or _prefer_local_file(candidate, current):
                rows_by_replay[replay_id] = candidate

        if rows_by_replay:
            con.executemany(
                """
                INSERT OR REPLACE INTO local_replay_index (
                    replay_id, local_file_path, file_size, file_mtime, discovered_at, last_seen_at, in_warehouse
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                list(rows_by_replay.values()),
            )
        return {
            "roots": [str(root) for root in roots],
            "scanned_files": scanned,
            "indexed_replays": len(rows_by_replay),
            "warehouse_replays": sum(1 for row in rows_by_replay.values() if row[6]),
            "orphan_local_replays": sum(1 for row in rows_by_replay.values() if not row[6]),
            "duplicates_collapsed": max(0, scanned - len(rows_by_replay)),
            "refreshed_at": discovered_at.isoformat(),
        }
    finally:
        con.close()


def backfill_replay_names(
    *,
    serving_db: Path | None = None,
    limit: int = 25,
    force: bool = False,
    refresh_index: bool = True,
    replay_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    serving_db = Path(serving_db or settings.serving_db)
    index_result = None
    if refresh_index:
        index_result = refresh_local_replay_index(serving_db=serving_db, replay_roots=replay_roots)

    con = duckdb.connect(str(serving_db))
    try:
        ensure_carball_schema(con)
        before = replay_name_coverage(con)
        candidates = select_backfill_candidates(con, limit=limit, force=force)
    finally:
        con.close()

    results: list[dict[str, Any]] = []
    parsed = 0
    cached = 0
    failed = 0
    missing = 0
    for candidate in candidates:
        replay_id = candidate["replay_id"]
        try:
            payload = ensure_replay_analysis(
                replay_id,
                local_file_path=candidate.get("local_file_path"),
                serving_db=serving_db,
                force=force,
            )
            results.append(payload)
            if payload.get("cached"):
                cached += 1
            else:
                parsed += 1
        except FileNotFoundError as exc:
            results.append({"replay_id": replay_id, "status": "missing_file", "error": str(exc)})
            missing += 1
        except ReplayParseError as exc:
            results.append({"replay_id": replay_id, "status": "failed", "error": str(exc)})
            failed += 1

    con = duckdb.connect(str(serving_db))
    try:
        after = replay_name_coverage(con)
    finally:
        con.close()

    return {
        "requested": len(candidates),
        "parsed": parsed,
        "cached": cached,
        "failed": failed,
        "missing_file": missing,
        "candidates": candidates,
        "results": results,
        "coverage_before": before,
        "coverage_after": after,
        "index": index_result,
    }


def select_backfill_candidates(
    con: duckdb.DuckDBPyConnection,
    *,
    limit: int = 25,
    force: bool = False,
) -> list[dict[str, Any]]:
    ensure_carball_schema(con)
    library_rows = _backfill_library_rows(con)
    status_rows = rows_to_dicts(
        con.execute(
            """
            SELECT replay_id, parser_version, status, file_size, file_mtime, parsed_at
            FROM replay_parsed_status
            """
        )
    ) if _table_exists(con, "replay_parsed_status") else []
    status_by_replay = {row["replay_id"]: row for row in status_rows}
    cooldown_cutoff = datetime.now(timezone.utc) - timedelta(seconds=get_settings().replay_parse_retry_seconds)

    candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for row in library_rows:
        replay_id = row["replay_id"]
        local_file_path = row.get("local_file_path")
        if not replay_id or not local_file_path or not Path(local_file_path).exists():
            continue
        status = status_by_replay.get(replay_id)
        if not force and status and _status_is_fresh(status, row):
            continue
        if not force and status and _status_is_recent_failure(status, row, cooldown_cutoff):
            continue
        rank = _candidate_rank(row, status)
        candidates.append((rank, row))

    candidates.sort(key=lambda item: item[0])
    return [row for _, row in candidates[: max(1, int(limit))]]


def replay_name_coverage(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    ensure_carball_schema(con)
    indexed_replays = int(
        con.execute("SELECT COUNT(*) FROM local_replay_index").fetchone()[0]
    ) if _table_exists(con, "local_replay_index") else 0
    warehouse_replays = int(
        con.execute("SELECT COUNT(*) FROM replays").fetchone()[0]
    ) if _table_exists(con, "replays") else 0
    orphan_local_replays = int(
        con.execute("SELECT COUNT(*) FROM local_replay_index WHERE NOT COALESCE(in_warehouse, FALSE)").fetchone()[0]
    ) if _table_exists(con, "local_replay_index") else 0
    parsed_replays = int(
        con.execute("SELECT COUNT(*) FROM replay_parsed_status WHERE status = 'completed'").fetchone()[0]
    ) if _table_exists(con, "replay_parsed_status") else 0
    failed_replays = int(
        con.execute("SELECT COUNT(*) FROM replay_parsed_status WHERE status = 'failed'").fetchone()[0]
    ) if _table_exists(con, "replay_parsed_status") else 0
    named_team_replays = int(
        con.execute(
            """
            SELECT COUNT(*)
            FROM replay_parsed_status
            WHERE status = 'completed'
              AND lower(trim(COALESCE(blue_team_name, ''))) NOT IN (?, ?, ?, ?)
              AND lower(trim(COALESCE(orange_team_name, ''))) NOT IN (?, ?, ?, ?)
            """,
            [
                *PLACEHOLDER_TEAM_KEYS,
                *PLACEHOLDER_TEAM_KEYS,
            ],
        ).fetchone()[0]
    ) if _table_exists(con, "replay_parsed_status") else 0
    named_player_replays = int(
        con.execute(
            """
            SELECT COUNT(DISTINCT replay_id)
            FROM replay_parsed_events
            WHERE player_name IS NOT NULL AND trim(player_name) <> ''
            """
        ).fetchone()[0]
    ) if _table_exists(con, "replay_parsed_events") else 0
    latest_parsed = rows_to_dicts(
        con.execute(
            """
            SELECT replay_id, blue_team_name, orange_team_name, parsed_at, status, error
            FROM replay_parsed_status
            ORDER BY COALESCE(parsed_at, last_accessed_at) DESC NULLS LAST, replay_id
            LIMIT 5
            """
        )
    ) if _table_exists(con, "replay_parsed_status") else []
    denominator = indexed_replays or warehouse_replays or max(parsed_replays, named_team_replays, 1)
    return {
        "indexed_local_replays": indexed_replays,
        "warehouse_replays": warehouse_replays,
        "orphan_local_replays": orphan_local_replays,
        "parsed_replays": parsed_replays,
        "failed_replays": failed_replays,
        "named_team_replays": named_team_replays,
        "named_player_replays": named_player_replays,
        "unparsed_replays": max(0, denominator - parsed_replays),
        "coverage_rate": round(named_team_replays / denominator, 4) if denominator else 0.0,
        "recent": latest_parsed,
    }


def repair_stale_running_parses(
    *,
    serving_db: Path | None = None,
    stale_after_seconds: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    serving_db = Path(serving_db or settings.serving_db)
    threshold_seconds = max(60, int(stale_after_seconds or settings.replay_parse_retry_seconds))
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=threshold_seconds)
    with duckdb.connect(str(serving_db)) as con:
        ensure_carball_schema(con)
        rows = rows_to_dicts(
            con.execute(
                """
                SELECT replay_id, local_file_path, last_accessed_at, parsed_at
                FROM replay_parsed_status
                WHERE status = 'running'
                  AND COALESCE(last_accessed_at, parsed_at, TIMESTAMP '1970-01-01') < ?
                ORDER BY COALESCE(last_accessed_at, parsed_at) ASC NULLS FIRST
                """,
                [cutoff],
            )
        )
        if rows:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            con.execute(
                """
                UPDATE replay_parsed_status
                SET status = 'failed',
                    error = 'Stale running parse repaired automatically',
                    parsed_at = COALESCE(parsed_at, ?),
                    last_accessed_at = ?
                WHERE status = 'running'
                  AND COALESCE(last_accessed_at, parsed_at, TIMESTAMP '1970-01-01') < ?
                """,
                [now, now, cutoff],
            )
        return {
            "cutoff": cutoff.isoformat(),
            "stale_running": len(rows),
            "repaired": len(rows),
            "sample": rows[:10],
        }


def ensure_replay_analysis(
    replay_id: str,
    *,
    local_file_path: str | Path | None = None,
    serving_db: Path | None = None,
    target_hz: int = 60,
    force: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    serving_db = Path(serving_db or settings.serving_db)
    replay_path = resolve_local_replay_path(replay_id, local_file_path)
    stat = replay_path.stat()
    now = datetime.now(timezone.utc)
    with duckdb.connect(str(serving_db)) as con:
        ensure_carball_schema(con)
        cached = con.execute(
            """
            SELECT parser_version, file_size, file_mtime, status, parsed_at, frame_count, duration_seconds, error
            FROM replay_parsed_status
            WHERE replay_id = ?
            """,
            [replay_id],
        ).fetchone()
        same_file = (
            cached
            and cached[0] == PARSER_VERSION
            and int(cached[1] or 0) == int(stat.st_size)
            and _same_mtime(cached[2], stat.st_mtime)
        )
        if same_file and not force and cached[3] == "completed":
            con.execute(
                "UPDATE replay_parsed_status SET last_accessed_at = ? WHERE replay_id = ?",
                [now, replay_id],
            )
            return {
                "replay_id": replay_id,
                "status": "completed",
                "cached": True,
                "frame_count": int(cached[5] or 0),
                "duration_seconds": float(cached[6] or 0.0),
                "parsed_at": cached[4].isoformat() if cached[4] else None,
                "source_hz": 30,
                "target_hz": target_hz,
            }
        if (
            same_file
            and not force
            and cached[3] == "failed"
            and cached[4]
            and (now - _coerce_utc(cached[4])).total_seconds() < settings.replay_parse_retry_seconds
        ):
            con.execute(
                "UPDATE replay_parsed_status SET last_accessed_at = ? WHERE replay_id = ?",
                [now, replay_id],
            )
            raise ReplayParseError(
                cached[7]
                or f"Replay parse for {replay_id} is cooling down after a recent failure."
            )

        con.execute(
            """
            INSERT OR REPLACE INTO replay_parsed_status (
                replay_id, local_file_path, parser_name, parser_version, source_hz, target_hz,
                frame_count, duration_seconds, blue_team_name, orange_team_name, blue_goals, orange_goals,
                file_size, file_mtime, parsed_at, parse_seconds, status, error, last_accessed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                replay_id,
                str(replay_path),
                "carball",
                PARSER_VERSION,
                None,
                target_hz,
                None,
                None,
                None,
                None,
                None,
                None,
                stat.st_size,
                float(stat.st_mtime),
                None,
                None,
                "running",
                None,
                now,
            ],
        )

    try:
        started = time.time()
        parsed = _parse_replay(replay_id, replay_path, target_hz=target_hz)
        parse_seconds = round(time.time() - started, 3)
    except Exception as exc:
        with duckdb.connect(str(serving_db)) as con:
            ensure_carball_schema(con)
            con.execute(
                """
                INSERT OR REPLACE INTO replay_parsed_status (
                    replay_id, local_file_path, parser_name, parser_version, source_hz, target_hz,
                    frame_count, duration_seconds, blue_team_name, orange_team_name, blue_goals, orange_goals,
                    file_size, file_mtime, parsed_at, parse_seconds, status, error, last_accessed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    replay_id,
                    str(replay_path),
                    "carball",
                    PARSER_VERSION,
                    None,
                    target_hz,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    stat.st_size if replay_path.exists() else None,
                    float(stat.st_mtime) if replay_path.exists() else None,
                    now,
                    None,
                    "failed",
                    str(exc),
                    now,
                ],
            )
        raise ReplayParseError(str(exc)) from exc

    with duckdb.connect(str(serving_db)) as con:
        ensure_carball_schema(con)
        con.execute("DELETE FROM replay_parsed_events WHERE replay_id = ?", [replay_id])
        for index, event in enumerate(parsed["events"], start=1):
            con.execute(
                """
                INSERT INTO replay_parsed_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    replay_id,
                    index,
                    event["t"],
                    event["event_type"],
                    event.get("team_color"),
                    event.get("team_id"),
                    event.get("player_id"),
                    event.get("player_name"),
                    event.get("other_team_color"),
                    event.get("other_team_id"),
                    event.get("other_player_id"),
                    event.get("other_player_name"),
                    event.get("value"),
                    _json(event.get("meta") or {}),
                ],
            )

        con.execute("DELETE FROM replay_parsed_frames WHERE replay_id = ?", [replay_id])
        con.execute(
            """
            INSERT INTO replay_parsed_frames VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                replay_id,
                zlib.compress(json.dumps(parsed["payload"], separators=(",", ":"), ensure_ascii=True).encode("utf-8"), level=6),
                _json(parsed["payload"].get("players") or []),
                _json(parsed["payload"].get("bounds") or {}),
                _json(
                    {
                        "event_count": len(parsed["events"]),
                        "duration_seconds": parsed["duration_seconds"],
                        "source_hz": parsed["source_hz"],
                        "target_hz": parsed["target_hz"],
                    }
                ),
                now,
            ],
        )
        con.execute(
            """
            INSERT OR REPLACE INTO replay_parsed_status (
                replay_id, local_file_path, parser_name, parser_version, source_hz, target_hz,
                frame_count, duration_seconds, blue_team_name, orange_team_name, blue_goals, orange_goals,
                file_size, file_mtime, parsed_at, parse_seconds, status, error, last_accessed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                replay_id,
                str(replay_path),
                "carball",
                PARSER_VERSION,
                parsed["source_hz"],
                parsed["target_hz"],
                parsed["payload"]["frame_count"],
                parsed["duration_seconds"],
                parsed["blue_team_name"],
                parsed["orange_team_name"],
                parsed["blue_goals"],
                parsed["orange_goals"],
                stat.st_size,
                float(stat.st_mtime),
                now,
                parse_seconds,
                "completed",
                None,
                now,
            ],
        )
    return {
        "replay_id": replay_id,
        "status": "completed",
        "cached": False,
        "frame_count": parsed["payload"]["frame_count"],
        "duration_seconds": parsed["duration_seconds"],
        "parsed_at": now.isoformat(),
        "parse_seconds": parse_seconds,
        "source_hz": parsed["source_hz"],
        "target_hz": parsed["target_hz"],
    }


def load_parsed_replay_frames(
    replay_id: str,
    *,
    hz: int = 8,
    max_frames: int = 1400,
    start_frame: int = 0,
    serving_db: Path | None = None,
    local_file_path: str | Path | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    serving_db = Path(serving_db or settings.serving_db)

    with database_connection(serving_db, read_only=True) as con:
        if not _table_exists(con, "replay_parsed_frames"):
            cached = None
        else:
            cached = con.execute(
                """
                SELECT pf.payload_zlib, ps.parser_version
                FROM replay_parsed_frames pf
                LEFT JOIN replay_parsed_status ps USING (replay_id)
                WHERE pf.replay_id = ?
                """,
                [replay_id],
            ).fetchone()
    cached_payload = None
    cached_version = None
    if cached and cached[0]:
        cached_payload = json.loads(zlib.decompress(cached[0]).decode("utf-8"))
        cached_version = cached[1]
    needs_refresh = (not cached_payload) or cached_version != PARSER_VERSION or _parsed_payload_needs_refresh(cached_payload)
    refresh_error: str | None = None
    if needs_refresh:
        try:
            ensure_replay_analysis(
                replay_id,
                local_file_path=local_file_path,
                serving_db=serving_db,
                force=bool(cached_payload and (cached_version != PARSER_VERSION or _parsed_payload_needs_refresh(cached_payload))),
            )
            with database_connection(serving_db, read_only=True) as con:
                cached = con.execute(
                    "SELECT payload_zlib FROM replay_parsed_frames WHERE replay_id = ?",
                    [replay_id],
                ).fetchone()
            cached_payload = None
        except ReplayParseError as exc:
            if not cached_payload:
                raise
            refresh_error = str(exc)
        except Exception as exc:
            if not cached_payload:
                raise
            refresh_error = str(exc)
    if cached and cached[0] and cached_payload is None:
        cached_payload = json.loads(zlib.decompress(cached[0]).decode("utf-8"))
    if not cached_payload:
        raise FileNotFoundError(f"No parsed replay telemetry found for {replay_id}")

    base_payload = cached_payload
    base_hz = int(base_payload.get("base_hz") or 60)
    sample_stride = max(1, round(base_hz / max(1, hz)))
    base_frames = base_payload.get("frames") or []
    base_offset = max(0, int(start_frame)) * sample_stride
    sampled = list(base_frames[base_offset::sample_stride][:max_frames])
    total_frame_count = math.ceil(len(base_frames) / sample_stride) if base_frames else 0
    return {
        "payload_version": PARSED_PAYLOAD_VERSION,
        "replay_id": replay_id,
        "source": "carball",
        "base_hz": base_hz,
        "sample_hz": max(1, round(base_hz / sample_stride)),
        "sample_stride": sample_stride,
        "start_frame": max(0, int(start_frame)),
        "frame_count": len(sampled),
        "total_frame_count": total_frame_count,
        "base_frame_count": int(base_payload.get("frame_count") or len(base_payload.get("frames") or [])),
        "bounds": base_payload.get("bounds") or {},
        "boost_pad_layout": base_payload.get("boost_pad_layout") or BOOST_PAD_LAYOUT,
        "players": base_payload.get("players") or [],
        "frames": sampled,
        "cache_stale": bool(refresh_error),
        "cache_warning": refresh_error,
    }


def _parsed_payload_needs_refresh(payload: dict[str, Any]) -> bool:
    if int(payload.get("payload_version") or 0) < PARSED_PAYLOAD_VERSION:
        return True
    players = payload.get("players") or []
    if not players:
        return True
    if not payload.get("boost_pad_layout"):
        return True
    return any(
        not player.get("car_name")
        and not player.get("car_family")
        and player.get("car_body_id") is None
        for player in players
    )


def parsed_events(con: duckdb.DuckDBPyConnection, replay_id: str) -> list[dict[str, Any]]:
    if not _table_exists(con, "replay_parsed_events"):
        return []
    return rows_to_dicts(
        con.execute(
            """
            SELECT
                event_id,
                replay_id,
                t,
                event_type,
                team_color,
                team_id,
                player_id,
                player_name,
                other_team_color,
                other_team_id,
                other_player_id,
                other_player_name,
                value,
                meta
            FROM replay_parsed_events
            WHERE replay_id = ?
            ORDER BY t, event_id
            """,
            [replay_id],
        )
    )


def parsed_status_map(con: duckdb.DuckDBPyConnection, replay_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not replay_ids or not _table_exists(con, "replay_parsed_status"):
        return {}
    placeholders = ", ".join("?" for _ in replay_ids)
    rows = rows_to_dicts(
        con.execute(
            f"""
            SELECT
                replay_id,
                status,
                parsed_at,
                target_hz,
                frame_count,
                error,
                duration_seconds,
                blue_team_name,
                orange_team_name,
                blue_goals,
                orange_goals,
                local_file_path
            FROM replay_parsed_status
            WHERE replay_id IN ({placeholders})
            """,
            replay_ids,
        )
    )
    return {row["replay_id"]: row for row in rows}


def _parse_replay(replay_id: str, replay_path: Path, *, target_hz: int) -> dict[str, Any]:
    analysis = _run_carball_analysis(replay_path)
    data_frame = analysis.get_data_frame()
    proto = MessageToDict(analysis.get_protobuf_data(), preserving_proto_field_name=True)

    players = _player_map(proto)
    team_names = _team_names(proto)
    normalized = _normalized_time_axis(data_frame)
    raw_times = normalized["times"]
    source_hz = normalized["source_hz"]
    duration_seconds = normalized["duration_seconds"]
    target_times = _target_times(duration_seconds, target_hz)
    payload = _build_frame_payload(replay_id, data_frame, players, team_names, raw_times, target_times, source_hz, target_hz)
    events = _build_events(replay_id, proto, players, team_names, raw_times)

    metadata = proto.get("game_metadata") or {}
    score = metadata.get("score") or {}
    return {
        "payload": payload,
        "events": events,
        "duration_seconds": duration_seconds,
        "source_hz": source_hz,
        "target_hz": target_hz,
        "blue_team_name": team_names.get(0, "Blue"),
        "orange_team_name": team_names.get(1, "Orange"),
        "blue_goals": int(score.get("team_0_score") or 0),
        "orange_goals": int(score.get("team_1_score") or 0),
    }


def _run_carball_analysis(replay_path: Path) -> Any:
    sink = io.StringIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with redirect_stdout(sink), redirect_stderr(sink):
            try:
                return carball.analyze_replay_file(str(replay_path), logging_level=50, calculate_intensive_events=False)
            except Exception:
                return _run_tolerant_carball_analysis(replay_path)


def _run_tolerant_carball_analysis(replay_path: Path) -> Any:
    loaded = carball.decompile_replay(str(replay_path))
    game = Game()
    game.initialize(loaded_json=loaded)
    analysis = AnalysisManager(game)
    player_map = analysis._get_game_metadata(analysis.game, analysis.protobuf_game)
    data_frame = analysis._initialize_data_frame(analysis.game)
    kickoff_frames, first_touch_frames = analysis._get_kickoff_frames(analysis.game, analysis.protobuf_game, data_frame)
    analysis.game.kickoff_frames = kickoff_frames
    try:
        if analysis._can_do_full_analysis(first_touch_frames):
            analysis._perform_full_analysis(
                analysis.game,
                analysis.protobuf_game,
                player_map,
                data_frame,
                kickoff_frames,
                first_touch_frames,
                calculate_intensive_events=False,
                clean=True,
            )
        else:
            analysis.protobuf_game.game_metadata.is_invalid_analysis = True
    except Exception:
        analysis.protobuf_game.game_metadata.is_invalid_analysis = True
    analysis._store_frames(data_frame)
    return analysis


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _player_map(proto: dict[str, Any]) -> dict[str, dict[str, Any]]:
    players: dict[str, dict[str, Any]] = {}
    for item in proto.get("players") or []:
        name = _clean_entity_name(item.get("name"))
        if not name or name in PLAYER_IGNORE:
            continue
        player_id = str(((item.get("id") or {}).get("id")) or name)
        team = 1 if bool(item.get("is_orange")) else 0
        profile = car_profile(car_body_id=(item.get("loadout") or {}).get("car"))
        players[player_id] = {
            "player_id": player_id,
            "player_name": name,
            "team": team,
            "team_name": "Orange" if team == 1 else "Blue",
            "column_key": name,
            "loadout": _normalize_loadout(item.get("loadout")),
            "camera_settings": _normalize_camera_settings(item.get("camera_settings")),
            "platform": item.get("platform"),
            **profile,
        }
    return players


def _normalize_loadout(loadout: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(loadout, dict):
        return None
    key_map = {
        "engine_audio": "engineAudio",
        "goal_explosion": "goalExplosion",
        "primary_color": "primaryColor",
        "accent_color": "accentColor",
        "primary_finish": "primaryFinish",
        "accent_finish": "accentFinish",
        "banner_paint": "bannerPaint",
        "boost_paint": "boostPaint",
        "car_paint": "carPaint",
        "goal_explosion_paint": "goalExplosionPaint",
        "skin_paint": "skinPaint",
        "trail_paint": "trailPaint",
        "wheels_paint": "wheelsPaint",
        "topper_paint": "topperPaint",
        "antenna_paint": "antennaPaint",
    }
    normalized: dict[str, Any] = {}
    for key, value in loadout.items():
        normalized[key_map.get(str(key), str(key))] = value
    return normalized


def _normalize_camera_settings(camera_settings: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(camera_settings, dict):
        return None
    key_map = {
        "field_of_view": "fieldOfView",
        "transition_speed": "transitionSpeed",
        "swivel_speed": "swivelSpeed",
    }
    normalized: dict[str, Any] = {}
    for key, value in camera_settings.items():
        normalized[key_map.get(str(key), str(key))] = value
    return normalized


def _team_names(proto: dict[str, Any]) -> dict[int, str]:
    team_names = {0: "Blue", 1: "Orange"}
    for team in proto.get("teams") or []:
        key = 1 if bool(team.get("is_orange")) else 0
        name = _clean_entity_name(team.get("name"))
        if name:
            team_names[key] = name
    return team_names


def _normalized_time_axis(data_frame: pd.DataFrame) -> dict[str, Any]:
    if ("game", "time") not in data_frame.columns:
        raise ReplayParseError("Carball did not expose a game time axis for this replay.")
    raw_times = pd.to_numeric(data_frame[("game", "time")], errors="coerce").to_numpy(dtype=float)
    raw_times = raw_times[np.isfinite(raw_times)]
    if raw_times.size < 2:
        raise ReplayParseError("Replay time axis is too small to resample.")
    start = float(raw_times[0])
    normalized = raw_times - start
    diffs = np.diff(normalized)
    finite_diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    median_delta = float(np.median(finite_diffs)) if finite_diffs.size else (1.0 / 30.0)
    if np.any(diffs < 0) or float(normalized[-1]) <= 0:
        normalized = np.arange(raw_times.size, dtype=float) * median_delta
    source_hz = max(1, int(round(1.0 / max(median_delta, 1e-6))))
    duration_seconds = round(float(normalized[-1]), 4)
    return {"times": normalized, "source_hz": source_hz, "duration_seconds": duration_seconds}


def _target_times(duration_seconds: float, target_hz: int) -> np.ndarray:
    if duration_seconds <= 0:
        return np.array([0.0], dtype=float)
    frame_step = 1.0 / max(1, target_hz)
    count = max(1, int(math.floor(duration_seconds / frame_step)) + 1)
    target_times = np.arange(count, dtype=float) * frame_step
    if target_times[-1] < duration_seconds:
        target_times = np.append(target_times, duration_seconds)
    return target_times


def _series_needs_boost_estimate(boost_values: np.ndarray, boost_active: np.ndarray) -> bool:
    finite = boost_values[np.isfinite(boost_values)]
    if finite.size == 0:
        return True
    active_any = bool(np.any(boost_active.astype(bool)))
    max_value = float(np.max(finite)) if finite.size else 0.0
    if max_value <= 1.0:
        return True
    nonzero = finite[np.abs(finite) > 0.1]
    if nonzero.size == 0 and active_any:
        return True
    return False


def _kickoff_like_frame(
    ball_x: float,
    ball_y: float,
    ball_z: float,
    ball_vel_x: float,
    ball_vel_y: float,
    ball_vel_z: float,
) -> bool:
    lateral_distance = math.hypot(ball_x, ball_y)
    speed = math.sqrt(ball_vel_x * ball_vel_x + ball_vel_y * ball_vel_y + ball_vel_z * ball_vel_z)
    return lateral_distance <= 140.0 and abs(ball_z - 92.75) <= 120.0 and speed <= 220.0


def _estimate_boost_and_pad_states(
    target_times: np.ndarray,
    car_payloads: dict[str, dict[str, Any]],
    *,
    ball_pos_x: np.ndarray,
    ball_pos_y: np.ndarray,
    ball_pos_z: np.ndarray,
    ball_vel_x: np.ndarray,
    ball_vel_y: np.ndarray,
    ball_vel_z: np.ndarray,
) -> tuple[dict[str, np.ndarray], list[list[bool]]]:
    frame_count = len(target_times)
    player_ids = list(car_payloads.keys())
    estimated_by_player = {
        player_id: np.zeros(frame_count, dtype=float)
        for player_id in player_ids
    }
    if not frame_count or not player_ids:
        return estimated_by_player, []

    next_available_at = np.zeros(len(BOOST_PAD_LAYOUT), dtype=float)
    pad_states: list[list[bool]] = []
    current_boost = {player_id: 33.0 for player_id in player_ids}
    last_positions = {
        player_id: (
            float(car_payloads[player_id]["pos_x"][0]),
            float(car_payloads[player_id]["pos_y"][0]),
        )
        for player_id in player_ids
    }

    for index, t_value in enumerate(target_times):
        if index == 0:
            dt = 1.0 / 60.0
        else:
            dt = max(1.0 / 120.0, float(t_value - target_times[index - 1]))

        kickoff_like = _kickoff_like_frame(
            float(ball_pos_x[index]),
            float(ball_pos_y[index]),
            float(ball_pos_z[index]),
            float(ball_vel_x[index]),
            float(ball_vel_y[index]),
            float(ball_vel_z[index]),
        )

        for player_id in player_ids:
            car = car_payloads[player_id]
            boost_active = bool(car["boost_active"][index])
            pos_x = float(car["pos_x"][index])
            pos_y = float(car["pos_y"][index])
            pos_z = float(car["pos_z"][index])
            previous_x, previous_y = last_positions[player_id]
            travel = math.hypot(pos_x - previous_x, pos_y - previous_y)
            last_positions[player_id] = (pos_x, pos_y)
            if kickoff_like and index > 0 and travel >= 1400.0:
                current_boost[player_id] = 33.0
            if boost_active and pos_z <= 300.0:
                current_boost[player_id] = max(
                    0.0,
                    current_boost[player_id] - (BOOST_BURN_PER_SECOND * dt),
                )

        active_row = [bool(t_value + 1e-6 >= next_available_at[pad_index]) for pad_index in range(len(BOOST_PAD_LAYOUT))]
        for pad_index, pad in enumerate(BOOST_PAD_LAYOUT):
            if not active_row[pad_index]:
                continue
            closest_player_id = None
            closest_distance = None
            for player_id in player_ids:
                car = car_payloads[player_id]
                pos_x = float(car["pos_x"][index])
                pos_y = float(car["pos_y"][index])
                pos_z = float(car["pos_z"][index])
                if pos_z > 260.0:
                    continue
                distance = math.hypot(pos_x - float(pad["x"]), pos_y - float(pad["y"]))
                if distance > float(pad["radius"]):
                    continue
                if closest_distance is None or distance < closest_distance:
                    closest_distance = distance
                    closest_player_id = player_id
            if closest_player_id is None:
                continue
            if bool(pad["full_boost"]):
                current_boost[closest_player_id] = BOOST_FULL_VALUE
            else:
                current_boost[closest_player_id] = min(
                    BOOST_FULL_VALUE,
                    current_boost[closest_player_id] + BOOST_SMALL_PAD_VALUE,
                )
            next_available_at[pad_index] = float(t_value) + float(pad["cooldown"])
            active_row[pad_index] = False

        pad_states.append(active_row)
        for player_id in player_ids:
            estimated_by_player[player_id][index] = round(
                max(0.0, min(BOOST_FULL_VALUE, current_boost[player_id])),
                2,
            )

    return estimated_by_player, pad_states


def _build_frame_payload(
    replay_id: str,
    data_frame: pd.DataFrame,
    players: dict[str, dict[str, Any]],
    team_names: dict[int, str],
    raw_times: np.ndarray,
    target_times: np.ndarray,
    source_hz: int,
    target_hz: int,
) -> dict[str, Any]:
    ball_pos_x = _resample_numeric(raw_times, _numeric_series(data_frame, ("ball", "pos_x")), target_times)
    ball_pos_y = _resample_numeric(raw_times, _numeric_series(data_frame, ("ball", "pos_y")), target_times)
    ball_pos_z = _resample_numeric(raw_times, _numeric_series(data_frame, ("ball", "pos_z"), default=92.75), target_times)
    ball_vel_x = _resample_numeric(raw_times, _numeric_series(data_frame, ("ball", "vel_x")), target_times)
    ball_vel_y = _resample_numeric(raw_times, _numeric_series(data_frame, ("ball", "vel_y")), target_times)
    ball_vel_z = _resample_numeric(raw_times, _numeric_series(data_frame, ("ball", "vel_z")), target_times)

    car_payloads: dict[str, dict[str, Any]] = {}
    for player_id, meta in players.items():
        column_key = meta["column_key"]
        if (column_key, "pos_x") not in data_frame.columns:
            continue
        team = int(meta["team"])
        raw_boost = _resample_numeric(raw_times, _numeric_series(data_frame, (column_key, "boost")), target_times)
        boost_active = _resample_bool(raw_times, _bool_series(data_frame, (column_key, "boost_active")), target_times)
        car_payloads[player_id] = {
            "player_id": player_id,
            "player_name": meta["player_name"],
            "team": team,
            "team_name": team_names.get(team, meta["team_name"]),
            "car_body_id": meta.get("car_body_id"),
            "car_name": meta.get("car_name"),
            "car_family": meta.get("car_family"),
            "pos_x": _resample_numeric(raw_times, _numeric_series(data_frame, (column_key, "pos_x")), target_times),
            "pos_y": _resample_numeric(raw_times, _numeric_series(data_frame, (column_key, "pos_y")), target_times),
            "pos_z": _resample_numeric(raw_times, _numeric_series(data_frame, (column_key, "pos_z"), default=17.0), target_times),
            "vel_x": _resample_numeric(raw_times, _numeric_series(data_frame, (column_key, "vel_x")), target_times),
            "vel_y": _resample_numeric(raw_times, _numeric_series(data_frame, (column_key, "vel_y")), target_times),
            "vel_z": _resample_numeric(raw_times, _numeric_series(data_frame, (column_key, "vel_z")), target_times),
            "rot_x": _resample_numeric(raw_times, _numeric_series(data_frame, (column_key, "rot_x")), target_times),
            "rot_y": _resample_numeric(raw_times, _numeric_series(data_frame, (column_key, "rot_y")), target_times),
            "rot_z": _resample_numeric(raw_times, _numeric_series(data_frame, (column_key, "rot_z")), target_times),
            "boost": raw_boost,
            "boost_active": boost_active,
            "dodge_active": _resample_bool(raw_times, _bool_series(data_frame, (column_key, "dodge_active")), target_times),
            "jump_active": _resample_bool(raw_times, _bool_series(data_frame, (column_key, "jump_active")), target_times),
        }

    estimated_boost_by_player, pad_states = _estimate_boost_and_pad_states(
        target_times,
        car_payloads,
        ball_pos_x=ball_pos_x,
        ball_pos_y=ball_pos_y,
        ball_pos_z=ball_pos_z,
        ball_vel_x=ball_vel_x,
        ball_vel_y=ball_vel_y,
        ball_vel_z=ball_vel_z,
    )
    for player_id, car in car_payloads.items():
        raw_boost = np.clip(np.asarray(car["boost"], dtype=float), 0.0, BOOST_FULL_VALUE)
        boost_active = np.asarray(car["boost_active"], dtype=bool)
        if _series_needs_boost_estimate(raw_boost, boost_active):
            car["boost"] = estimated_boost_by_player.get(player_id, raw_boost)
            car["boost_source"] = "estimated"
        else:
            car["boost"] = raw_boost
            car["boost_source"] = "carball"

    bounds = {
        "min_x": math.inf,
        "max_x": -math.inf,
        "min_y": math.inf,
        "max_y": -math.inf,
        "min_z": math.inf,
        "max_z": -math.inf,
    }
    frames: list[dict[str, Any]] = []
    for index, t_value in enumerate(target_times):
        ball_pos = [round(float(ball_pos_x[index]), 2), round(float(ball_pos_y[index]), 2), round(float(ball_pos_z[index]), 2)]
        _expand_bounds(bounds, ball_pos)
        cars = []
        for player_id, car in car_payloads.items():
            pos = [round(float(car["pos_x"][index]), 2), round(float(car["pos_y"][index]), 2), round(float(car["pos_z"][index]), 2)]
            _expand_bounds(bounds, pos)
            jump_active = bool(car["jump_active"][index])
            dodge_active = bool(car["dodge_active"][index])
            cars.append(
                {
                    "player_id": player_id,
                    "player_name": car["player_name"],
                    "team": car["team"],
                    "team_name": car["team_name"],
                    "car_body_id": car.get("car_body_id"),
                    "car_name": car.get("car_name"),
                    "car_family": car.get("car_family"),
                    "boost": round(float(car["boost"][index]), 2),
                    "boost_source": car.get("boost_source", "carball"),
                    "demo": False,
                    "on_ground": bool(pos[2] <= 25.0 and abs(car["vel_z"][index]) < 200.0),
                    "has_flip": not dodge_active,
                    "pos": pos,
                    "vel": [
                        round(float(car["vel_x"][index]), 2),
                        round(float(car["vel_y"][index]), 2),
                        round(float(car["vel_z"][index]), 2),
                    ],
                    "euler": [
                        round(float(car["rot_x"][index]), 4),
                        round(float(car["rot_y"][index]), 4),
                        round(float(car["rot_z"][index]), 4),
                    ],
                    "jump_active": jump_active,
                    "dodge_active": dodge_active,
                    "ball_cam": None,
                }
            )
        frames.append(
            {
                "bucket": index,
                "t": round(float(t_value), 4),
                "ball": {
                    "pos": ball_pos,
                    "vel": [
                        round(float(ball_vel_x[index]), 2),
                        round(float(ball_vel_y[index]), 2),
                        round(float(ball_vel_z[index]), 2),
                    ],
                },
                "pad_states": pad_states[index] if index < len(pad_states) else [],
                "cars": cars,
            }
        )

    return {
        "payload_version": PARSED_PAYLOAD_VERSION,
        "replay_id": replay_id,
        "source": "carball",
        "source_hz": source_hz,
        "base_hz": target_hz,
        "frame_count": len(frames),
        "bounds": {key: 0.0 if math.isinf(value) else round(value, 2) for key, value in bounds.items()},
        "boost_pad_layout": BOOST_PAD_LAYOUT,
        "players": [
            {
                "player_id": player_id,
                "player_name": meta["player_name"],
                "team": meta["team"],
                "team_name": team_names.get(int(meta["team"]), meta["team_name"]),
                "car_body_id": meta.get("car_body_id"),
                "car_name": meta.get("car_name"),
                "car_family": meta.get("car_family"),
                "loadout": meta.get("loadout"),
                "camera_settings": meta.get("camera_settings"),
                "platform": meta.get("platform"),
            }
            for player_id, meta in players.items()
            if player_id in car_payloads
        ],
        "frames": frames,
    }


def _build_events(
    replay_id: str,
    proto: dict[str, Any],
    players: dict[str, dict[str, Any]],
    team_names: dict[int, str],
    raw_times: np.ndarray,
) -> list[dict[str, Any]]:
    metadata = proto.get("game_metadata") or {}
    hits = sorted((proto.get("game_stats") or {}).get("hits") or [], key=lambda item: int(item.get("frame_number") or 0))
    events: list[dict[str, Any]] = []
    previous_touch: dict[str, Any] | None = None

    for hit in hits:
        frame_number = int(hit.get("frame_number") or 0)
        t_value = round(_frame_to_t(raw_times, frame_number), 4)
        player_id = str(((hit.get("player_id") or {}).get("id")) or "")
        player = players.get(player_id, {"player_id": player_id or None, "player_name": player_id or "Unknown", "team": None, "team_name": None})
        team = player.get("team")
        team_color = "orange" if team == 1 else "blue" if team == 0 else None
        other_team_color = None
        if previous_touch and previous_touch.get("team_color") in {"blue", "orange"}:
            other_team_color = previous_touch["team_color"]

        touch_meta = {
            "frame_number": frame_number,
            "distance_to_goal": float(hit.get("distance_to_goal") or 0.0),
            "distance": float(hit.get("distance") or 0.0),
            "aerial": bool(hit.get("aerial")),
            "shot": bool(hit.get("shot")),
            "goal": bool(hit.get("goal")),
            "is_kickoff": bool(hit.get("is_kickoff")),
        }
        events.append(
            _event(
                replay_id,
                t_value,
                "touch",
                team_color=team_color,
                player_id=player.get("player_id"),
                player_name=player.get("player_name"),
                other_team_color=other_team_color,
                value=1.0,
                meta=touch_meta,
            )
        )

        if previous_touch is None:
            events.append(
                _event(
                    replay_id,
                    t_value,
                    "possession_start",
                    team_color=team_color,
                    player_id=player.get("player_id"),
                    player_name=player.get("player_name"),
                    value=1.0,
                    meta={"reason": "first_touch"},
                )
            )
        else:
            gap = t_value - float(previous_touch["t"])
            loose_ball_recovery = gap >= 3.0
            if loose_ball_recovery:
                events.append(
                    _event(
                        replay_id,
                        round(max(previous_touch["t"], t_value - 0.01), 4),
                        "loose_ball_start",
                        team_color=None,
                        value=gap,
                        meta={"gap_seconds": round(gap, 3)},
                    )
                )
            turnover_like = (
                team_color
                and team_color != previous_touch.get("team_color")
                and not loose_ball_recovery
                and gap > 0.35
                and not bool(hit.get("is_kickoff"))
                and not bool(previous_touch.get("is_kickoff"))
            )
            if turnover_like:
                events.append(
                    _event(
                        replay_id,
                        t_value,
                        "possession_end",
                        team_color=previous_touch.get("team_color"),
                        player_id=previous_touch.get("player_id"),
                        player_name=previous_touch.get("player_name"),
                        other_team_color=team_color,
                        value=gap,
                        meta={"ended_by": "turnover"},
                    )
                )
                events.append(
                    _event(
                        replay_id,
                        t_value,
                        "turnover",
                        team_color=team_color,
                        player_id=player.get("player_id"),
                        player_name=player.get("player_name"),
                        other_team_color=previous_touch.get("team_color"),
                        other_player_id=previous_touch.get("player_id"),
                        other_player_name=previous_touch.get("player_name"),
                        value=1.0,
                        meta={
                            "gap_seconds": round(gap, 3),
                            "frame_number": frame_number,
                            "distance_to_goal": float(hit.get("distance_to_goal") or 0.0),
                            "shot": bool(hit.get("shot")),
                        },
                    )
                )
                events.append(
                    _event(
                        replay_id,
                        t_value,
                        "possession_start",
                        team_color=team_color,
                        player_id=player.get("player_id"),
                        player_name=player.get("player_name"),
                        other_team_color=previous_touch.get("team_color"),
                        value=1.0,
                        meta={"reason": "touch_change"},
                    )
                )
            elif loose_ball_recovery and team_color:
                events.append(
                    _event(
                        replay_id,
                        t_value,
                        "possession_start",
                        team_color=team_color,
                        player_id=player.get("player_id"),
                        player_name=player.get("player_name"),
                        other_team_color=previous_touch.get("team_color"),
                        value=1.0,
                        meta={"reason": "loose_ball_recovery"},
                    )
                )

        if bool(hit.get("is_kickoff")) and team_color:
            events.append(
                _event(
                    replay_id,
                    t_value,
                    "kickoff_outcome",
                    team_color=team_color,
                    player_id=player.get("player_id"),
                    player_name=player.get("player_name"),
                    value=1.0,
                    meta={"frame_number": frame_number},
                )
            )

        distance_to_goal = float(hit.get("distance_to_goal") or 0.0)
        if team_color and (bool(hit.get("shot")) or distance_to_goal <= 3600.0):
            pressure_value = 1.2 if bool(hit.get("shot")) else max(0.25, 1.0 - min(distance_to_goal / 7000.0, 1.0))
            events.append(
                _event(
                    replay_id,
                    t_value,
                    "pressure_phase",
                    team_color=team_color,
                    player_id=player.get("player_id"),
                    player_name=player.get("player_name"),
                    value=round(pressure_value, 4),
                    meta={"distance_to_goal": round(distance_to_goal, 3), "shot": bool(hit.get("shot"))},
                )
            )

        if bool(hit.get("goal")) and team_color:
            events.append(
                _event(
                    replay_id,
                    t_value,
                    "goal",
                    team_color=team_color,
                    player_id=player.get("player_id"),
                    player_name=player.get("player_name"),
                    value=1.0,
                    meta={"frame_number": frame_number},
                )
            )

        previous_touch = {
            "t": t_value,
            "team_color": team_color,
            "player_id": player.get("player_id"),
            "player_name": player.get("player_name"),
            "is_kickoff": bool(hit.get("is_kickoff")),
        }

    duration = float(metadata.get("length") or 0.0)
    if previous_touch and duration > previous_touch["t"]:
        events.append(
            _event(
                replay_id,
                round(duration, 4),
                "possession_end",
                team_color=previous_touch.get("team_color"),
                player_id=previous_touch.get("player_id"),
                player_name=previous_touch.get("player_name"),
                value=round(duration - previous_touch["t"], 4),
                meta={"ended_by": "match_end"},
            )
        )
    return events


def _event(
    replay_id: str,
    t_value: float,
    event_type: str,
    *,
    team_color: str | None = None,
    player_id: str | None = None,
    player_name: str | None = None,
    other_team_color: str | None = None,
    other_player_id: str | None = None,
    other_player_name: str | None = None,
    value: float | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "replay_id": replay_id,
        "t": round(float(t_value), 4),
        "event_type": event_type,
        "team_color": team_color,
        "team_id": f"{replay_id}:{team_color}" if team_color in {"blue", "orange"} else None,
        "player_id": player_id,
        "player_name": player_name,
        "other_team_color": other_team_color,
        "other_team_id": f"{replay_id}:{other_team_color}" if other_team_color in {"blue", "orange"} else None,
        "other_player_id": other_player_id,
        "other_player_name": other_player_name,
        "value": None if value is None else round(float(value), 4),
        "meta": meta or {},
    }


def _numeric_series(data_frame: pd.DataFrame, column: tuple[str, str], *, default: float = 0.0) -> np.ndarray:
    if column not in data_frame.columns:
        return np.full(len(data_frame), default, dtype=float)
    values = pd.to_numeric(data_frame[column], errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(values).any():
        return np.full(len(values), default, dtype=float)
    series = pd.Series(values)
    return series.interpolate(limit_direction="both").fillna(default).to_numpy(dtype=float)


def _bool_series(data_frame: pd.DataFrame, column: tuple[str, str]) -> np.ndarray:
    if column not in data_frame.columns:
        return np.zeros(len(data_frame), dtype=bool)
    series = pd.Series(data_frame[column], copy=False)
    return np.asarray([_coerce_bool_like(value) for value in series.tolist()], dtype=bool)


def _coerce_bool_like(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return bool(int(value))
    if isinstance(value, (float, np.floating)):
        if not np.isfinite(value):
            return False
        return bool(float(value))
    if pd.isna(value):
        return False
    text = str(value).strip().casefold()
    if text in {"", "false", "f", "0", "no", "n", "off", "null", "none", "nan"}:
        return False
    if text in {"true", "t", "1", "yes", "y", "on"}:
        return True
    try:
        return bool(float(text))
    except ValueError:
        return False


def _resample_numeric(raw_times: np.ndarray, values: np.ndarray, target_times: np.ndarray) -> np.ndarray:
    mask = np.isfinite(raw_times) & np.isfinite(values)
    if mask.sum() == 0:
        return np.zeros(len(target_times), dtype=float)
    if mask.sum() == 1:
        return np.full(len(target_times), float(values[mask][0]), dtype=float)
    return np.interp(target_times, raw_times[mask], values[mask], left=float(values[mask][0]), right=float(values[mask][-1]))


def _resample_bool(raw_times: np.ndarray, values: np.ndarray, target_times: np.ndarray) -> np.ndarray:
    if not len(values):
        return np.zeros(len(target_times), dtype=bool)
    indices = np.searchsorted(raw_times, target_times, side="right") - 1
    indices = np.clip(indices, 0, len(values) - 1)
    return values[indices]


def _frame_to_t(raw_times: np.ndarray, frame_number: int) -> float:
    index = min(max(int(frame_number) - 1, 0), len(raw_times) - 1)
    return float(raw_times[index])


def _expand_bounds(bounds: dict[str, float], pos: list[float]) -> None:
    bounds["min_x"] = min(bounds["min_x"], pos[0])
    bounds["max_x"] = max(bounds["max_x"], pos[0])
    bounds["min_y"] = min(bounds["min_y"], pos[1])
    bounds["max_y"] = max(bounds["max_y"], pos[1])
    bounds["min_z"] = min(bounds["min_z"], pos[2])
    bounds["max_z"] = max(bounds["max_z"], pos[2])


def _clean_entity_name(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    if any(marker in text for marker in ("Ã", "ã", "â", "ð")):
        try:
            repaired = text.encode("latin1").decode("utf-8")
        except UnicodeError:
            repaired = text
        else:
            if repaired.strip():
                text = repaired.strip()
    return text


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _same_mtime(left: Any, right: float) -> bool:
    try:
        return abs(float(left) - float(right)) <= 0.001
    except (TypeError, ValueError):
        return False


def _table_exists(con: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = con.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'main' AND table_name = ?
        """,
        [table_name],
    ).fetchone()
    return row is not None


def _replay_roots(replay_roots: list[str | Path] | None = None) -> list[Path]:
    settings = get_settings()
    roots = [Path(root) for root in (replay_roots or [PROJECT_ROOT / "replays", settings.replay_download_dir])]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve()) if root.exists() else str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _iter_replay_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for replay_path in root.rglob("*.replay"):
            key = str(replay_path.resolve())
            if key in seen:
                continue
            seen.add(key)
            files.append(replay_path)
    return files


def _prefer_local_file(candidate: list[Any], current: list[Any]) -> bool:
    if int(candidate[2] or 0) != int(current[2] or 0):
        return int(candidate[2] or 0) > int(current[2] or 0)
    if not _same_mtime(candidate[3], float(current[3] or 0.0)):
        return float(candidate[3] or 0.0) > float(current[3] or 0.0)
    return str(candidate[1]) < str(current[1])


def _backfill_library_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    if _table_exists(con, "local_replay_index"):
        rows = rows_to_dicts(
            con.execute(
                """
                SELECT replay_id, local_file_path, file_size, file_mtime, last_seen_at, COALESCE(in_warehouse, FALSE) AS in_warehouse
                FROM local_replay_index
                WHERE local_file_path IS NOT NULL
                """
            )
        )
        if rows:
            return rows

    rows: list[dict[str, Any]] = []
    if _table_exists(con, "replays"):
        for (replay_id,) in con.execute("SELECT replay_id FROM replays").fetchall():
            local_path = _fallback_local_path(replay_id)
            if not local_path:
                continue
            stat = local_path.stat()
            rows.append(
                {
                    "replay_id": replay_id,
                    "local_file_path": str(local_path),
                    "file_size": int(stat.st_size),
                    "file_mtime": float(stat.st_mtime),
                    "last_seen_at": None,
                    "in_warehouse": True,
                }
            )
    return rows


def _fallback_local_path(replay_id: str) -> Path | None:
    try:
        return resolve_local_replay_path(replay_id)
    except FileNotFoundError:
        return None


def _status_is_fresh(status: dict[str, Any], row: dict[str, Any]) -> bool:
    return (
        status.get("status") == "completed"
        and status.get("parser_version") == PARSER_VERSION
        and int(status.get("file_size") or 0) == int(row.get("file_size") or 0)
        and _same_mtime(status.get("file_mtime"), float(row.get("file_mtime") or 0.0))
    )


def _status_is_recent_failure(status: dict[str, Any], row: dict[str, Any], cooldown_cutoff: datetime) -> bool:
    if status.get("status") != "failed" or status.get("parser_version") != PARSER_VERSION:
        return False
    if int(status.get("file_size") or 0) != int(row.get("file_size") or 0):
        return False
    if not _same_mtime(status.get("file_mtime"), float(row.get("file_mtime") or 0.0)):
        return False
    parsed_at = status.get("parsed_at")
    if not parsed_at:
        return False
    if isinstance(parsed_at, datetime):
        parsed_moment = _coerce_utc(parsed_at)
    elif isinstance(parsed_at, str):
        try:
            parsed_moment = _coerce_utc(datetime.fromisoformat(parsed_at.replace("Z", "+00:00")))
        except ValueError:
            parsed_moment = None
    else:
        parsed_moment = None
    return bool(parsed_moment and parsed_moment >= cooldown_cutoff)


def _candidate_rank(row: dict[str, Any], status: dict[str, Any] | None) -> tuple[Any, ...]:
    if not status:
        status_rank = 0
    elif status.get("status") == "failed":
        status_rank = 1
    else:
        status_rank = 2
    last_seen = row.get("last_seen_at")
    if isinstance(last_seen, datetime):
        sort_time = -last_seen.timestamp()
    elif isinstance(last_seen, str):
        try:
            sort_time = -datetime.fromisoformat(last_seen.replace("Z", "+00:00")).timestamp()
        except ValueError:
            sort_time = -float(row.get("file_mtime") or 0.0)
    else:
        sort_time = -float(row.get("file_mtime") or 0.0)
    return (
        0 if row.get("in_warehouse") else 1,
        status_rank,
        sort_time,
        -int(row.get("file_size") or 0),
        row.get("replay_id") or "",
    )
