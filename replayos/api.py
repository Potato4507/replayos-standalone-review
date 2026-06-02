from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response

from .analyst import answer_question, matchup_report_markdown
from .analytics import compare_teams, run_model_pipeline
from .ballchasing import (
    BallchasingClient,
    BallchasingError,
    configured_ballchasing_sources,
    resolve_ballchasing_source,
    sync_ballchasing_replays,
    sync_ballchasing_source_set,
)
from .carball_ingest import PARSER_BACKEND_VERSION, PARSER_VERSION, ReplayParseError, backfill_replay_names, refresh_local_replay_index, replay_name_coverage
from .live_sync import LiveSyncError, live_status, site_live, sync_live_data
from .maintenance import MaintenanceSidecarProcess, MaintenanceWorker
from .native_viewer import (
    build_native_viewer_payload,
    load_native_viewer_payload_cache,
    native_viewer_gzip_response,
    store_native_viewer_payload_cache,
)
from .config import PROJECT_ROOT, get_settings
from .db import jsonable, refresh_read_replica, rows_to_dicts, serving_connection
from .frames import load_replay_frames
from .models import AnalystQuery, BallchasingSyncRequest, CarballBackfillRequest, EvalBackfillRequest, MaintenanceRunRequest, RefreshRequest, YouTubeSyncRequest
from .semantics import build_replay_timeline
from .site import (
    ballchasing_status,
    get_library_replay,
    get_series,
    library_replay_page,
    list_series,
    refresh_replay_review_cache,
    replay_viewer,
    replay_review_status,
    site_home as site_home_summary,
    table_exists,
    team_elo_index,
)
from .recordbook import head_to_head, player_record_profile, recordbook_overview, team_record_profile
from .warehouse import refresh_warehouse
from .youtube_sync import YouTubeClient, YouTubeSyncError, replay_videos, sync_youtube_videos, youtube_status
from .carball_ingest import load_parsed_replay_frames, parsed_events


settings = get_settings()
app = FastAPI(title=settings.api_title, version="0.1.0")
maintenance_worker = MaintenanceWorker()
maintenance_sidecar = MaintenanceSidecarProcess()
RL_LOADOUT_ASSET_HOST = "https://storage.googleapis.com/rl-loadout"
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _start_maintenance_worker() -> None:
    try:
        refresh_read_replica(get_settings().serving_db)
    except Exception:
        pass
    maintenance_worker.start()
    maintenance_sidecar.start()


@app.on_event("shutdown")
def _stop_maintenance_worker() -> None:
    maintenance_worker.stop()
    maintenance_sidecar.stop()


def get_con() -> duckdb.DuckDBPyConnection:
    db_path = get_settings().serving_db
    if not db_path.exists():
        raise HTTPException(status_code=503, detail=f"Serving database missing at {db_path}. Run scripts/build_warehouse.py.")
    with serving_connection(read_only=True) as con:
        yield con


def _fetch(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    return rows_to_dicts(con.execute(sql, params or []))


def _decode_prediction(row: dict[str, Any]) -> dict[str, Any]:
    if "reasons_json" in row:
        try:
            row["reasons"] = json.loads(row.pop("reasons_json") or "[]")
        except json.JSONDecodeError:
            row["reasons"] = []
    return {key: jsonable(value) for key, value in row.items()}


def _maintenance_status(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    settings = get_settings()
    ball = ballchasing_status(con)
    carball = replay_name_coverage(con)
    eval_status = replay_review_status(con)
    live = live_status(con)
    youtube = youtube_status(con)
    youtube.update(YouTubeClient.provider_status(api_key=settings.youtube_api_key))
    return {
        "ballchasing": {
            **ball,
            "token_configured": bool(settings.ballchasing_api_token),
            "default_group": settings.ballchasing_default_group,
        },
        "carball": {
            **carball,
            "parser_version": PARSER_VERSION,
            "parser_backend_version": PARSER_BACKEND_VERSION,
            "batch_recommended": min(250, max(24, int(carball.get("unparsed_replays") or 0))),
        },
        "eval": eval_status,
        "youtube": youtube,
        "live": live,
        "health": {
            "serving_db": str(settings.serving_db),
            "replay_download_dir": str(settings.replay_download_dir),
            "live_refresh_due": bool((live.get("stale") if isinstance(live, dict) else False)),
            "parse_backlog": int(carball.get("unparsed_replays") or 0),
            "review_backlog": int(eval_status.get("missing_replays") or 0),
        },
    }


def _refresh_replica_after_write() -> None:
    try:
        refresh_read_replica(get_settings().serving_db)
    except Exception:
        pass


def _ensure_background_upkeep() -> None:
    try:
        maintenance_worker.start()
    except Exception:
        pass
    try:
        maintenance_sidecar.start()
    except Exception:
        pass


def _safe_rl_loadout_asset_path(asset_path: str) -> str:
    normalized = Path(asset_path.replace("\\", "/"))
    parts = [part for part in normalized.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(status_code=400, detail="Invalid asset path.")
    return "/".join(parts)


def _rl_loadout_cache_path(asset_path: str) -> Path:
    return PROJECT_ROOT / "cache" / "rl-loadout" / Path(*asset_path.split("/"))


def _download_rl_loadout_asset(asset_path: str, cache_path: Path) -> tuple[Path, str | None]:
    url = f"{RL_LOADOUT_ASSET_HOST}/{asset_path}"
    request = Request(url, headers={"User-Agent": "ReplayOS/0.1"})
    try:
        with urlopen(request, timeout=45) as response:
            payload = response.read()
            content_type = response.headers.get_content_type()
    except HTTPError as exc:
        if exc.code == 404:
            raise HTTPException(status_code=404, detail=f"RL loadout asset not found: {asset_path}") from exc
        raise HTTPException(status_code=502, detail=f"RL loadout asset fetch failed: {exc.code}") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail=f"RL loadout asset fetch failed: {exc.reason}") from exc

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(payload)
    return cache_path, content_type


@app.get("/health")
def health() -> dict[str, Any]:
    current = get_settings()
    return {
        "status": "ok",
        "raw_db_exists": current.raw_db.exists(),
        "serving_db_exists": current.serving_db.exists(),
        "raw_db": str(current.raw_db),
        "serving_db": str(current.serving_db),
    }


@app.get("/assets/rl-loadout/{asset_path:path}")
def rl_loadout_asset(asset_path: str) -> FileResponse:
    safe_path = _safe_rl_loadout_asset_path(asset_path)
    cache_path = _rl_loadout_cache_path(safe_path)
    content_type = mimetypes.guess_type(cache_path.name)[0]
    if not cache_path.exists():
        cache_path, downloaded_type = _download_rl_loadout_asset(safe_path, cache_path)
        content_type = downloaded_type or content_type
    return FileResponse(path=cache_path, media_type=content_type)


@app.get("/summary")
def summary(con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    tables = ["replays", "matches", "teams", "players", "events", "features_replay", "predictions", "model_versions"]
    counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    if table_exists(con, "remote_replays"):
        counts["remote_replays"] = con.execute("SELECT COUNT(*) FROM remote_replays").fetchone()[0]
    if table_exists(con, "remote_groups"):
        counts["remote_groups"] = con.execute("SELECT COUNT(*) FROM remote_groups").fetchone()[0]
    if table_exists(con, "replay_parsed_status"):
        counts["parsed_replays"] = con.execute("SELECT COUNT(*) FROM replay_parsed_status WHERE status = 'completed'").fetchone()[0]
    if table_exists(con, "live_streams"):
        counts["live_streams"] = con.execute("SELECT COUNT(*) FROM live_streams").fetchone()[0]
    event_counts = _fetch(
        con,
        """
        SELECT event_type, COUNT(*) AS n
        FROM events
        GROUP BY event_type
        ORDER BY n DESC
        LIMIT 10
        """,
    )
    latest_model = _fetch(
        con,
        """
        SELECT model_version_id, name, model_type, target, metrics_json, created_at
        FROM model_versions
        ORDER BY created_at DESC
        LIMIT 1
        """,
    )
    return {"counts": counts, "event_counts": event_counts, "latest_model": latest_model[0] if latest_model else None}


@app.get("/warehouse/schema")
def warehouse_schema(con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    columns = _fetch(
        con,
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'main'
        ORDER BY table_name, ordinal_position
        """,
    )
    lineage = _fetch(con, "SELECT * FROM lineage ORDER BY table_name")
    return {"columns": columns, "lineage": lineage}


@app.get("/sources/ballchasing/status")
def source_ballchasing_status(con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    status = ballchasing_status(con)
    settings = get_settings()
    defaults = configured_ballchasing_sources()
    status["token_configured"] = bool(settings.ballchasing_api_token)
    status["default_group"] = settings.ballchasing_default_group
    status["default_groups"] = list(defaults["groups"])
    status["default_creators"] = list(defaults["creators"])
    status["default_creator_group_limit"] = settings.ballchasing_default_creator_group_limit
    if settings.ballchasing_api_token:
        try:
            ping = BallchasingClient().ping()
            status["api_ping"] = {"ok": True, "name": ping.get("name"), "type": ping.get("type")}
        except BallchasingError as exc:
            status["api_ping"] = {"ok": False, "error": str(exc)}
    else:
        status["api_ping"] = {"ok": False, "error": "BALLCHASING_API_TOKEN is not configured"}
    return status


@app.post("/sources/ballchasing/sync")
def source_ballchasing_sync(payload: BallchasingSyncRequest) -> dict[str, Any]:
    settings = get_settings()
    filters: dict[str, Any] = {}
    if payload.playlist:
        filters["playlist"] = payload.playlist
    if payload.player_name:
        filters["player-name"] = payload.player_name
    if payload.player_id:
        filters["player-id"] = payload.player_id
    try:
        resolved_group = None
        resolved_creator = None
        if payload.group_id:
            resolved = resolve_ballchasing_source(payload.group_id)
            if resolved:
                if resolved[0] == "creator":
                    resolved_creator = resolved[1]
                else:
                    resolved_group = resolved[1]
            else:
                resolved_group = payload.group_id
        if payload.creator_id:
            resolved = resolve_ballchasing_source(payload.creator_id)
            if resolved and resolved[0] == "creator":
                resolved_creator = resolved[1]
            else:
                resolved_creator = payload.creator_id
        if resolved_group or resolved_creator:
            result = sync_ballchasing_source_set(
                settings.serving_db,
                group_ids=[resolved_group] if resolved_group else None,
                creator_ids=[resolved_creator] if resolved_creator else None,
                base_filters=filters,
                count=payload.count,
                download_files=payload.download_files,
                fetch_details=payload.fetch_details,
                force_download=payload.force_download,
                parse_downloads=payload.parse_downloads,
            )
            _refresh_replica_after_write()
            return result
        defaults = configured_ballchasing_sources()
        if not resolved_group and not resolved_creator and (defaults["groups"] or defaults["creators"]):
            result = sync_ballchasing_source_set(
                settings.serving_db,
                group_ids=list(defaults["groups"]),
                creator_ids=list(defaults["creators"]),
                creator_group_limit=settings.ballchasing_default_creator_group_limit,
                base_filters=filters,
                count=payload.count,
                download_files=payload.download_files,
                fetch_details=payload.fetch_details,
                force_download=payload.force_download,
                parse_downloads=payload.parse_downloads,
            )
            _refresh_replica_after_write()
            return result
        result = sync_ballchasing_replays(
            settings.serving_db,
            filters=filters,
            count=payload.count,
            download_files=payload.download_files,
            fetch_details=payload.fetch_details,
            force_download=payload.force_download,
            parse_downloads=payload.parse_downloads,
        )
        _refresh_replica_after_write()
        return result
    except BallchasingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/sources/carball/status")
def source_carball_status(con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    status = replay_name_coverage(con)
    status["parser_version"] = PARSER_VERSION
    status["parser_backend_version"] = PARSER_BACKEND_VERSION
    status["replay_download_dir"] = str(get_settings().replay_download_dir)
    return status


@app.post("/sources/carball/index")
def source_carball_index() -> dict[str, Any]:
    result = refresh_local_replay_index(serving_db=get_settings().serving_db)
    _refresh_replica_after_write()
    return result


@app.post("/sources/carball/backfill")
def source_carball_backfill(payload: CarballBackfillRequest) -> dict[str, Any]:
    result = backfill_replay_names(
        serving_db=get_settings().serving_db,
        limit=payload.limit,
        force=payload.force,
        refresh_index=payload.refresh_index,
    )
    _refresh_replica_after_write()
    return result


@app.get("/sources/maintenance/status")
def source_maintenance_status(con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    _ensure_background_upkeep()
    return {
        **_maintenance_status(con),
        "worker": maintenance_worker.snapshot(),
        "sidecar": maintenance_sidecar.snapshot(),
    }


@app.post("/sources/maintenance/run")
def source_maintenance_run(payload: MaintenanceRunRequest) -> dict[str, Any]:
    result = maintenance_worker.run_now(
        trigger="manual",
        refresh_index=payload.refresh_index,
        refresh_ballchasing=payload.refresh_ballchasing,
        backfill_names=payload.backfill_names,
        refresh_youtube=payload.refresh_youtube,
        backfill_eval=payload.backfill_eval,
        refresh_live=payload.refresh_live,
        parse_limit=payload.parse_limit,
        eval_limit=payload.eval_limit,
        ballchasing_count=payload.ballchasing_count,
        youtube_limit=payload.youtube_limit,
        force_eval=payload.force_eval,
    )
    if result.get("status") == "busy":
        return result
    _refresh_replica_after_write()
    with serving_connection(read_only=True) as con:
        result["status"] = {
            **_maintenance_status(con),
            "worker": maintenance_worker.snapshot(),
            "sidecar": maintenance_sidecar.snapshot(),
        }
    return result


@app.get("/sources/eval/status")
def source_eval_status(con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    return replay_review_status(con)


@app.post("/sources/eval/backfill")
def source_eval_backfill(payload: EvalBackfillRequest) -> dict[str, Any]:
    with duckdb.connect(str(get_settings().serving_db)) as con:
        result = refresh_replay_review_cache(con, limit=payload.limit, force=payload.force)
    _refresh_replica_after_write()
    return result


@app.get("/sources/youtube/status")
def source_youtube_status(con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    status = youtube_status(con)
    settings = get_settings()
    status.update(YouTubeClient.provider_status(api_key=settings.youtube_api_key))
    status["api_key_configured"] = bool(settings.youtube_api_key)
    return status


@app.post("/sources/youtube/sync")
def source_youtube_sync(payload: YouTubeSyncRequest) -> dict[str, Any]:
    current = get_settings()
    try:
        result = sync_youtube_videos(current.serving_db, replay_id=payload.replay_id, limit=payload.limit)
        _refresh_replica_after_write()
        return result
    except YouTubeSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/sources/live/status")
def source_live_status(con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    return live_status(con)


@app.post("/sources/live/sync")
def source_live_sync(force: bool = False) -> dict[str, Any]:
    try:
        result = sync_live_data(get_settings().serving_db, force=force)
        _refresh_replica_after_write()
        return result
    except LiveSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/site/home")
def site_home_payload() -> dict[str, Any]:
    with serving_connection(read_only=True) as con:
        return site_home_summary(con)


@app.get("/site/live")
def site_live_payload(refresh_if_stale: bool = False) -> dict[str, Any]:
    current = get_settings()
    if refresh_if_stale:
        try:
            sync_live_data(current.serving_db, force=False)
        except (LiveSyncError, duckdb.Error):
            pass
    with serving_connection(read_only=True) as con:
        return site_live(con)


@app.get("/site/teams/elo")
def site_team_elo(
    limit: int = Query(default=16, ge=1, le=100),
) -> dict[str, Any]:
    with serving_connection(read_only=True) as con:
        return {"items": team_elo_index(con, limit=limit)}


@app.get("/site/records")
def site_records(
    limit: int = Query(default=8, ge=1, le=20),
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    return recordbook_overview(con, limit=limit)


@app.get("/site/records/team")
def site_team_record(
    name: str,
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    try:
        return team_record_profile(con, name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/site/records/player")
def site_player_record(
    name: str,
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    try:
        return player_record_profile(con, name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/site/records/head-to-head")
def site_head_to_head(
    kind: str = Query(pattern="^(team|player)$"),
    left: str = Query(min_length=1),
    right: str = Query(min_length=1),
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    try:
        return head_to_head(con, kind=kind, left_name=left, right_name=right)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/series")
def site_series(
    limit: int = Query(default=12, ge=1, le=100),
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    return {"items": list_series(con, limit=limit)}


@app.get("/series/{group_id}")
def site_series_detail(group_id: str, con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    series = get_series(con, group_id)
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    return series


@app.get("/library/replays")
def library_replays(
    limit: int = Query(default=24, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    group_id: str | None = None,
    search: str | None = None,
    parsed_only: bool = False,
    review_ready: bool = False,
    sort: str = Query(default="recent"),
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    return library_replay_page(
        con,
        limit=limit,
        offset=offset,
        group_id=group_id,
        search=search,
        parsed_only=parsed_only,
        review_ready=review_ready,
        sort_mode=sort,
    )


@app.get("/library/replays/{replay_id}")
def library_replay(replay_id: str, con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    replay = get_library_replay(con, replay_id)
    if replay is None:
        raise HTTPException(status_code=404, detail="Replay not found in downloaded library")
    return replay


@app.get("/library/replays/{replay_id}/videos")
def library_replay_videos(replay_id: str, con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    return {"items": replay_videos(con, replay_id)}


@app.get("/library/replays/{replay_id}/viewer")
def library_replay_viewer(replay_id: str) -> dict[str, Any]:
    with serving_connection(read_only=True) as con:
        viewer = replay_viewer(con, replay_id)
        if viewer is None:
            raise HTTPException(status_code=404, detail="Replay not found in downloaded library")
        return viewer


@app.get("/library/replays/{replay_id}/frames")
def library_replay_frames(
    replay_id: str,
    hz: int = Query(default=60, ge=1, le=60),
    max_frames: int = Query(default=1800, ge=10, le=3000),
    start_frame: int = Query(default=0, ge=0, le=250000),
) -> dict[str, Any]:
    try:
        return load_replay_frames(replay_id, hz=hz, max_frames=max_frames, start_frame=start_frame)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/library/replays/{replay_id}/native-viewer")
def library_replay_native_viewer(
    request: Request,
    replay_id: str,
    hz: int = Query(default=60, ge=1, le=60),
    max_frames: int = Query(default=24000, ge=300, le=250000),
    start_frame: int = Query(default=0, ge=0, le=250000),
) -> Response:
    settings = get_settings()
    with serving_connection(read_only=True) as con:
        replay = get_library_replay(con, replay_id)
    if replay is None:
        raise HTTPException(status_code=404, detail="Replay not found in downloaded library")
    cached_payload = load_native_viewer_payload_cache(
        replay_id,
        hz=hz,
        max_frames=max_frames,
        start_frame=start_frame,
        parser_version=PARSER_VERSION,
    )
    if cached_payload:
        return native_viewer_gzip_response(cached_payload, request)
    parse_status = str((replay.get("carball_status") or {}).get("status") or "").lower()
    if parse_status == "failed":
        raise HTTPException(
            status_code=409,
            detail=str((replay.get("carball_status") or {}).get("error") or "Native viewer parse failed for this replay."),
        )
    if parse_status == "running":
        raise HTTPException(
            status_code=409,
            detail="This replay is still being parsed into native-viewer telemetry.",
        )
    try:
        parsed_payload = load_parsed_replay_frames(
            replay_id,
            hz=hz,
            max_frames=max_frames,
            start_frame=start_frame,
            serving_db=settings.serving_db,
            local_file_path=replay.get("local_file_path"),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReplayParseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Native viewer parse is unavailable right now: {exc}") from exc
    parsed_replay_id = str(parsed_payload.get("replay_id") or "")
    if parsed_replay_id and parsed_replay_id != str(replay_id):
        raise HTTPException(
            status_code=409,
            detail=f"Parsed telemetry replay mismatch: requested {replay_id}, got {parsed_replay_id}",
        )
    with serving_connection(read_only=True) as con:
        events = parsed_events(con, replay_id)
    try:
        payload = build_native_viewer_payload(replay, parsed_payload, events)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    encoded_payload = store_native_viewer_payload_cache(
        replay_id,
        payload,
        hz=hz,
        max_frames=max_frames,
        start_frame=start_frame,
        parser_version=PARSER_VERSION,
    )
    return native_viewer_gzip_response(encoded_payload, request)


@app.get("/library/replays/{replay_id}/file")
def library_replay_file(replay_id: str, con: duckdb.DuckDBPyConnection = Depends(get_con)) -> FileResponse:
    replay = get_library_replay(con, replay_id)
    if replay is None:
        raise HTTPException(status_code=404, detail="Replay not found in downloaded library")
    local_file = replay.get("local_file_path")
    if not local_file or not Path(local_file).exists():
        raise HTTPException(status_code=404, detail="Replay file is not downloaded locally")
    return FileResponse(path=local_file, media_type="application/octet-stream", filename=f"{replay_id}.replay")


@app.get("/replays")
def list_replays(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    rows = _fetch(
        con,
        """
        SELECT r.replay_id, r.ingested_at, r.game_duration, r.frame_total_frames, r.has_semantic_features,
               m.blue_goals, m.orange_goals, m.winner_team_id
        FROM replays r
        LEFT JOIN matches m USING (replay_id)
        ORDER BY r.has_semantic_features DESC, r.ingested_at DESC NULLS LAST, r.replay_id
        LIMIT ? OFFSET ?
        """,
        [limit, offset],
    )
    return {"items": rows, "limit": limit, "offset": offset}


@app.get("/replays/{replay_id}")
def get_replay(replay_id: str, con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    rows = _fetch(
        con,
        """
        SELECT r.*, m.blue_goals, m.orange_goals, m.winner_team_id
        FROM replays r
        LEFT JOIN matches m USING (replay_id)
        WHERE r.replay_id = ?
        """,
        [replay_id],
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Replay not found")
    return rows[0]


@app.get("/replays/{replay_id}/events")
def replay_events(
    replay_id: str,
    event_type: str | None = None,
    limit: int = Query(default=250, ge=1, le=2000),
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    params: list[Any] = [replay_id]
    where = "WHERE replay_id = ?"
    if event_type:
        where += " AND event_type = ?"
        params.append(event_type)
    params.append(limit)
    rows = _fetch(
        con,
        f"""
        SELECT event_id, replay_id, t, event_type, team_color, team_id, player_id, player_name,
               other_team_color, other_team_id, value, meta
        FROM events
        {where}
        ORDER BY t, event_id
        LIMIT ?
        """,
        params,
    )
    return {"items": rows, "timeline": build_replay_timeline(rows)}


@app.get("/teams")
def list_teams(
    limit: int = Query(default=100, ge=1, le=500),
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    rows = _fetch(
        con,
        """
        SELECT t.*, ft.possession_rate, ft.pressure_rate, ft.touch_rate, ft.clutch_boost_advantage
        FROM teams t
        LEFT JOIN features_team_match ft USING (team_id)
        ORDER BY t.win_result DESC NULLS LAST, t.goals_for DESC, t.team_id
        LIMIT ?
        """,
        [limit],
    )
    return {"items": rows}


@app.get("/teams/{team_id}")
def get_team(team_id: str, con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    rows = _fetch(
        con,
        """
        SELECT t.*, ft.*
        FROM teams t
        LEFT JOIN features_team_match ft USING (team_id)
        WHERE t.team_id = ?
        """,
        [team_id],
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Team not found")
    predictions = _fetch(
        con,
        """
        SELECT model_version_id, prediction_type, predicted_label, probability, score, reasons_json
        FROM predictions
        WHERE target_id = ?
        ORDER BY created_at DESC
        """,
        [team_id],
    )
    rows[0]["predictions"] = [_decode_prediction(prediction) for prediction in predictions]
    return rows[0]


@app.get("/players")
def list_players(
    limit: int = Query(default=100, ge=1, le=500),
    search: str | None = None,
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    params: list[Any] = []
    where = ""
    if search:
        where = "WHERE player_id ILIKE ? OR player_name ILIKE ?"
        params.extend([f"%{search}%", f"%{search}%"])
    params.append(limit)
    rows = _fetch(
        con,
        f"""
        SELECT *
        FROM players
        {where}
        ORDER BY goals DESC, touches DESC, player_id
        LIMIT ?
        """,
        params,
    )
    return {"items": rows}


@app.get("/players/{player_id}")
def get_player(player_id: str, con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    rows = _fetch(con, "SELECT * FROM players WHERE player_id = ?", [player_id])
    if not rows:
        raise HTTPException(status_code=404, detail="Player not found")
    matches = _fetch(
        con,
        """
        SELECT *
        FROM features_player_match
        WHERE player_id = ?
        ORDER BY impact_score DESC
        LIMIT 100
        """,
        [player_id],
    )
    rows[0]["matches"] = matches
    return rows[0]


@app.get("/features/replay/{replay_id}")
def replay_features(replay_id: str, con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    rows = _fetch(con, "SELECT * FROM features_replay WHERE replay_id = ?", [replay_id])
    if not rows:
        raise HTTPException(status_code=404, detail="Replay features not found")
    return rows[0]


@app.get("/features/team-match")
def team_match_features(
    replay_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    params: list[Any] = []
    where = ""
    if replay_id:
        where = "WHERE replay_id = ?"
        params.append(replay_id)
    params.append(limit)
    rows = _fetch(con, f"SELECT * FROM features_team_match {where} ORDER BY replay_id, team_color LIMIT ?", params)
    return {"items": rows}


@app.get("/predictions")
def list_predictions(
    replay_id: str | None = None,
    target_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    filters = []
    params: list[Any] = []
    if replay_id:
        filters.append("replay_id = ?")
        params.append(replay_id)
    if target_id:
        filters.append("target_id = ?")
        params.append(target_id)
    where = "WHERE " + " AND ".join(filters) if filters else ""
    params.append(limit)
    rows = _fetch(
        con,
        f"""
        SELECT *
        FROM predictions
        {where}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    )
    return {"items": [_decode_prediction(row) for row in rows]}


@app.get("/model-versions")
def model_versions(con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    rows = _fetch(con, "SELECT * FROM model_versions ORDER BY created_at DESC")
    for row in rows:
        for key in ("features_json", "metrics_json", "calibration_json", "artifact_json"):
            row[key.replace("_json", "")] = json.loads(row.pop(key) or "null")
    return {"items": rows}


@app.get("/matchups/compare")
def matchup_compare(
    team_a_id: str,
    team_b_id: str,
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> dict[str, Any]:
    try:
        return compare_teams(con, team_a_id, team_b_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/analyst/query")
def analyst_query(payload: AnalystQuery, con: duckdb.DuckDBPyConnection = Depends(get_con)) -> dict[str, Any]:
    return answer_question(con, payload.question, replay_id=payload.replay_id)


@app.get("/reports/matchup", response_class=PlainTextResponse)
def matchup_report(
    team_a_id: str,
    team_b_id: str,
    con: duckdb.DuckDBPyConnection = Depends(get_con),
) -> str:
    try:
        matchup = compare_teams(con, team_a_id, team_b_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return matchup_report_markdown(matchup)


@app.post("/jobs/refresh")
def refresh_job(payload: RefreshRequest) -> dict[str, Any]:
    current = get_settings()
    warehouse_result = refresh_warehouse(current.raw_db, current.serving_db, sample_limit=payload.sample_limit)
    model_result = run_model_pipeline(current.serving_db) if payload.train_models else None
    _refresh_replica_after_write()
    return {"warehouse": warehouse_result, "models": model_result}
