from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb

from replayos.ballchasing import BallchasingError, configured_ballchasing_sources, sync_ballchasing_source_set
from replayos.carball_ingest import backfill_replay_names, refresh_local_replay_index, replay_name_coverage
from replayos.config import get_settings
from replayos.live_sync import LiveSyncError, sync_live_data
from replayos.site import refresh_replay_review_cache, replay_review_status
from replayos.youtube_sync import YouTubeSyncError, sync_youtube_videos, youtube_status


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _safe_count(con: duckdb.DuckDBPyConnection, table_name: str, where_sql: str = "", params: list[Any] | None = None) -> int:
    if not _table_exists(con, table_name):
        return 0
    sql = f"SELECT COUNT(*) FROM {table_name} {where_sql}".strip()
    return int(con.execute(sql, params or []).fetchone()[0])


def _rows_to_dicts(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [item[0] for item in cursor.description]
    rows = cursor.fetchall()
    return [dict(zip(columns, row, strict=False)) for row in rows]


def repair_stale_running_parses(
    serving_db: Path,
    *,
    stale_after_seconds: int = 60 * 60,
) -> dict[str, Any]:
    cutoff = _now() - timedelta(seconds=max(60, int(stale_after_seconds)))
    with duckdb.connect(str(serving_db)) as con:
        if not _table_exists(con, "replay_parsed_status"):
            return {"stale_running": 0, "repaired": 0, "sample": []}
        rows = _rows_to_dicts(
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
            con.execute(
                """
                UPDATE replay_parsed_status
                SET status = 'failed',
                    error = 'Stale running parse repaired by full_library_run.py',
                    parsed_at = COALESCE(parsed_at, ?),
                    last_accessed_at = ?
                WHERE status = 'running'
                  AND COALESCE(last_accessed_at, parsed_at, TIMESTAMP '1970-01-01') < ?
                """,
                [_now(), _now(), cutoff],
            )
        return {
            "stale_running": len(rows),
            "repaired": len(rows),
            "sample": rows[:10],
            "cutoff": cutoff.isoformat(),
        }


def audit_library(serving_db: Path) -> dict[str, Any]:
    with duckdb.connect(str(serving_db), read_only=True) as con:
        coverage = replay_name_coverage(con)
        review = replay_review_status(con)
        youtube = youtube_status(con)

        local_rows = []
        if _table_exists(con, "local_replay_index"):
            local_rows = _rows_to_dicts(
                con.execute(
                    """
                    SELECT replay_id, local_file_path, file_size, file_mtime, last_seen_at, in_warehouse
                    FROM local_replay_index
                    ORDER BY replay_id
                    """
                )
            )

        missing_files = []
        seen_paths: dict[str, int] = {}
        for row in local_rows:
            path_value = row.get("local_file_path")
            if not path_value:
                continue
            normalized = str(Path(path_value))
            seen_paths[normalized] = seen_paths.get(normalized, 0) + 1
            if not Path(normalized).exists():
                missing_files.append(
                    {
                        "replay_id": row.get("replay_id"),
                        "local_file_path": normalized,
                    }
                )

        duplicate_paths = [
            {"local_file_path": path, "count": count}
            for path, count in seen_paths.items()
            if count > 1
        ]

        stale_running = _rows_to_dicts(
            con.execute(
                """
                SELECT replay_id, local_file_path, last_accessed_at, parsed_at
                FROM replay_parsed_status
                WHERE status = 'running'
                ORDER BY COALESCE(last_accessed_at, parsed_at) ASC NULLS FIRST
                LIMIT 25
                """
            )
        ) if _table_exists(con, "replay_parsed_status") else []

        completed_missing_payload = _safe_count(
            con,
            "replay_parsed_status",
            """
            ps
            LEFT JOIN replay_parsed_frames pf USING (replay_id)
            WHERE ps.status = 'completed' AND pf.replay_id IS NULL
            """,
        ) if _table_exists(con, "replay_parsed_frames") else 0

        remote_missing_files = []
        if _table_exists(con, "remote_replays"):
            remote_rows = _rows_to_dicts(
                con.execute(
                    """
                    SELECT replay_id, local_file_path
                    FROM remote_replays
                    WHERE local_file_path IS NOT NULL AND trim(local_file_path) <> ''
                    """
                )
            )
            for row in remote_rows:
                path_value = row.get("local_file_path")
                if path_value and not Path(str(path_value)).exists():
                    remote_missing_files.append(
                        {
                            "replay_id": row.get("replay_id"),
                            "local_file_path": str(path_value),
                        }
                    )

        status_counts = _rows_to_dicts(
            con.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM replay_parsed_status
                GROUP BY 1
                ORDER BY n DESC, status
                """
            )
        ) if _table_exists(con, "replay_parsed_status") else []

        remote_counts = {
            "remote_replays": _safe_count(con, "remote_replays"),
            "remote_groups": _safe_count(con, "remote_groups"),
            "remote_players": _safe_count(con, "remote_players"),
        }

        return {
            "audited_at": _now().isoformat(),
            "coverage": coverage,
            "review": review,
            "youtube": {
                "videos": youtube.get("videos", 0),
                "sync_enabled": youtube.get("sync_enabled", False),
                "provider": youtube.get("provider"),
            },
            "counts": {
                "replays": _safe_count(con, "replays"),
                "local_replay_index": _safe_count(con, "local_replay_index"),
                "replay_parsed_frames": _safe_count(con, "replay_parsed_frames"),
                "replay_review_cache": _safe_count(con, "replay_review_cache"),
                **remote_counts,
            },
            "issues": {
                "missing_local_files": len(missing_files),
                "duplicate_local_paths": len(duplicate_paths),
                "stale_running_rows": len(stale_running),
                "completed_missing_payload": completed_missing_payload,
                "remote_missing_local_files": len(remote_missing_files),
            },
            "samples": {
                "missing_local_files": missing_files[:15],
                "duplicate_local_paths": duplicate_paths[:15],
                "stale_running_rows": stale_running[:15],
                "remote_missing_local_files": remote_missing_files[:15],
                "parsed_status_counts": status_counts,
            },
        }


def catch_up_reviews(serving_db: Path, *, batch_size: int, max_cycles: int, force: bool) -> dict[str, Any]:
    cycles: list[dict[str, Any]] = []
    for index in range(max(0, int(max_cycles))):
        with duckdb.connect(str(serving_db)) as con:
            result = refresh_replay_review_cache(con, limit=max(1, int(batch_size)), force=force)
        cycles.append({"cycle": index + 1, **result})
        status = result.get("status") or {}
        if result.get("processed", 0) == 0:
            break
        if int(status.get("missing_replays") or 0) <= 0:
            break
        if result.get("computed", 0) == 0 and result.get("cached", 0) == 0:
            break
    return {
        "cycles": cycles,
        "last_status": cycles[-1]["status"] if cycles else None,
    }


def catch_up_youtube(
    serving_db: Path,
    *,
    batch_size: int,
    max_cycles: int,
) -> dict[str, Any]:
    cycles: list[dict[str, Any]] = []
    for index in range(max(0, int(max_cycles))):
        try:
            result = sync_youtube_videos(serving_db=serving_db, limit=max(1, int(batch_size)))
        except YouTubeSyncError as exc:
            cycles.append({"cycle": index + 1, "status": "error", "error": str(exc)})
            break
        cycles.append({"cycle": index + 1, **result})
        if result.get("replay_count", 0) == 0:
            break
        if result.get("linked", 0) == 0 and result.get("segmented", 0) == 0:
            break
    return {
        "cycles": cycles,
        "status": cycles[-1] if cycles else None,
    }


def run_full_pass(
    *,
    parse_batch: int,
    parse_cycles: int,
    eval_batch: int,
    eval_cycles: int,
    youtube_batch: int,
    youtube_cycles: int,
    ballchasing_count: int,
    skip_ballchasing: bool,
    skip_youtube: bool,
    skip_live: bool,
    stale_running_seconds: int,
    force_eval: bool,
) -> dict[str, Any]:
    settings = get_settings()
    started_at = _now()
    results: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "settings": {
            "serving_db": str(settings.serving_db),
            "replay_download_dir": str(settings.replay_download_dir),
            "parse_batch": parse_batch,
            "parse_cycles": parse_cycles,
            "eval_batch": eval_batch,
            "eval_cycles": eval_cycles,
            "youtube_batch": youtube_batch,
            "youtube_cycles": youtube_cycles,
            "ballchasing_count": ballchasing_count,
        },
        "before": audit_library(settings.serving_db),
        "steps": {},
    }

    results["steps"]["repair_stale_running"] = repair_stale_running_parses(
        settings.serving_db,
        stale_after_seconds=stale_running_seconds,
    )

    results["steps"]["index"] = refresh_local_replay_index(serving_db=settings.serving_db)

    if not skip_live:
        try:
            results["steps"]["live"] = sync_live_data(settings.serving_db, force=True)
        except LiveSyncError as exc:
            results["steps"]["live"] = {"status": "error", "error": str(exc)}

    sources = configured_ballchasing_sources()
    if skip_ballchasing:
        results["steps"]["ballchasing"] = {"status": "skipped", "reason": "skip_ballchasing flag set"}
    elif not settings.ballchasing_api_token:
        results["steps"]["ballchasing"] = {"status": "skipped", "reason": "BALLCHASING_API_TOKEN is not configured"}
    elif not (sources["groups"] or sources["creators"]):
        results["steps"]["ballchasing"] = {"status": "skipped", "reason": "No default Ballchasing sources are configured"}
    else:
        try:
            results["steps"]["ballchasing"] = sync_ballchasing_source_set(
                serving_db=settings.serving_db,
                group_ids=sources["groups"],
                creator_ids=sources["creators"],
                creator_group_limit=settings.ballchasing_default_creator_group_limit,
                count=max(1, int(ballchasing_count)),
                download_files=True,
                fetch_details=True,
                force_download=False,
                parse_downloads=True,
            )
        except BallchasingError as exc:
            results["steps"]["ballchasing"] = {"status": "error", "error": str(exc)}

    parse_runs: list[dict[str, Any]] = []
    for cycle in range(max(0, int(parse_cycles))):
        batch_result = backfill_replay_names(
            serving_db=settings.serving_db,
            limit=max(1, int(parse_batch)),
            force=False,
            refresh_index=False,
        )
        parse_runs.append({"cycle": cycle + 1, **batch_result})
        if batch_result.get("requested", 0) == 0:
            break
        if (
            batch_result.get("parsed", 0) == 0
            and batch_result.get("cached", 0) == 0
            and batch_result.get("failed", 0) == 0
            and batch_result.get("missing_file", 0) == 0
        ):
            break
    results["steps"]["carball"] = {
        "cycles": parse_runs,
        "last_coverage": parse_runs[-1]["coverage_after"] if parse_runs else None,
    }

    results["steps"]["eval"] = catch_up_reviews(
        settings.serving_db,
        batch_size=eval_batch,
        max_cycles=eval_cycles,
        force=force_eval,
    )

    if skip_youtube:
        results["steps"]["youtube"] = {"status": "skipped", "reason": "skip_youtube flag set"}
    else:
        results["steps"]["youtube"] = catch_up_youtube(
            settings.serving_db,
            batch_size=youtube_batch,
            max_cycles=youtube_cycles,
        )

    results["after"] = audit_library(settings.serving_db)
    completed_at = _now()
    results["completed_at"] = completed_at.isoformat()
    results["duration_seconds"] = round((completed_at - started_at).total_seconds(), 3)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a full ReplayOS library catch-up and audit pass.")
    parser.add_argument("--parse-batch", type=int, default=8)
    parser.add_argument("--parse-cycles", type=int, default=3)
    parser.add_argument("--eval-batch", type=int, default=96)
    parser.add_argument("--eval-cycles", type=int, default=4)
    parser.add_argument("--youtube-batch", type=int, default=8)
    parser.add_argument("--youtube-cycles", type=int, default=2)
    parser.add_argument("--ballchasing-count", type=int, default=50)
    parser.add_argument("--stale-running-seconds", type=int, default=60 * 60)
    parser.add_argument("--sleep-seconds", type=int, default=180)
    parser.add_argument("--loop", action="store_true", help="Keep running catch-up passes forever with sleeps between them.")
    parser.add_argument("--skip-ballchasing", action="store_true")
    parser.add_argument("--skip-youtube", action="store_true")
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--force-eval", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--json-output", type=Path, default=None)
    args = parser.parse_args()

    settings = get_settings()

    def emit(payload: dict[str, Any]) -> None:
        rendered = json.dumps(payload, indent=2, default=str)
        print(rendered)
        if args.json_output:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(rendered, encoding="utf-8")

    if args.audit_only:
        emit(audit_library(settings.serving_db))
        return

    while True:
        payload = run_full_pass(
            parse_batch=args.parse_batch,
            parse_cycles=args.parse_cycles,
            eval_batch=args.eval_batch,
            eval_cycles=args.eval_cycles,
            youtube_batch=args.youtube_batch,
            youtube_cycles=args.youtube_cycles,
            ballchasing_count=args.ballchasing_count,
            skip_ballchasing=args.skip_ballchasing,
            skip_youtube=args.skip_youtube,
            skip_live=args.skip_live,
            stale_running_seconds=args.stale_running_seconds,
            force_eval=args.force_eval,
        )
        emit(payload)
        if not args.loop:
            return
        time.sleep(max(30, int(args.sleep_seconds)))


if __name__ == "__main__":
    main()
