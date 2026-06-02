from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import duckdb
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from replayos.ballchasing import (
    BallchasingError,
    ensure_ballchasing_replay_download,
    normalize_ballchasing_creator_id,
    normalize_ballchasing_group_id,
    resolve_ballchasing_source,
    sync_ballchasing_source_set,
)
from replayos.carball_ingest import PARSER_VERSION, ReplayParseError, load_parsed_replay_frames, parsed_events, refresh_local_replay_index
from replayos.config import PROJECT_ROOT, get_settings
from replayos.native_viewer import (
    build_native_viewer_payload,
    load_native_viewer_payload_cache,
    native_viewer_gzip_response,
    store_native_viewer_payload_cache,
)
from replayos.site import get_library_replay, library_replay_page, replay_viewer, table_exists


APP_TITLE = "ReplayOS Review Platform"
PLATFORM_ROOT = PROJECT_ROOT / "output" / "review-platform"
NATIVE_VIEWER_DIR = PROJECT_ROOT / "frontend" / "public" / "native-viewer"
DEFAULT_CONFIG = {
    "ballchasing_api_token": "",
    "group_ids": [],
    "creator_ids": [],
    "auto_sync_enabled": False,
    "auto_sync_minutes": 30,
    "sync_count": 8,
    "creator_group_limit": 12,
}

WORKSPACE_LOCK = threading.RLock()
STATE_LOCK = threading.Lock()
AUTO_SYNC_STOP = threading.Event()
AUTO_SYNC_THREAD: threading.Thread | None = None
SYNC_STATE: dict[str, Any] = {
    "running": False,
    "last_started_at": None,
    "last_completed_at": None,
    "last_trigger": None,
    "last_error": None,
    "last_result": None,
}


class ReviewPlatformError(RuntimeError):
    """Raised when the review platform cannot complete a requested operation."""


class ConfigUpdateRequest(BaseModel):
    ballchasing_api_token: str | None = Field(default=None, max_length=200)
    clear_ballchasing_api_token: bool = False
    group_sources_text: str = Field(default="", max_length=4000)
    creator_sources_text: str = Field(default="", max_length=4000)
    auto_sync_enabled: bool = False
    auto_sync_minutes: int = Field(default=30, ge=5, le=1440)
    sync_count: int = Field(default=8, ge=1, le=200)
    creator_group_limit: int = Field(default=12, ge=1, le=200)


class ReviewRequest(BaseModel):
    replay_input: str = Field(min_length=3, max_length=300)
    ballchasing_api_token: str | None = Field(default=None, max_length=200)
    force_refresh: bool = False


class SourceSyncRequest(BaseModel):
    count: int | None = Field(default=None, ge=1, le=200)
    force_download: bool = False


app = FastAPI(title=APP_TITLE)

if NATIVE_VIEWER_DIR.exists():
    app.mount("/native-viewer", StaticFiles(directory=str(NATIVE_VIEWER_DIR), html=True), name="native-viewer")


def platform_paths() -> dict[str, Path]:
    return {
        "root": PLATFORM_ROOT,
        "serving_db": PLATFORM_ROOT / "review-platform.duckdb",
        "download_dir": PLATFORM_ROOT / "replays",
        "config_file": PLATFORM_ROOT / "platform-config.json",
    }


def ensure_platform_dirs() -> dict[str, Path]:
    paths = platform_paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    paths["download_dir"].mkdir(parents=True, exist_ok=True)
    return paths


def normalize_replay_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Replay id is required.")
    match = re.search(r"/replay[s]?/([a-f0-9-]{8,})", raw, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    match = re.search(r"([a-f0-9]{8}-[a-f0-9-]{27,})", raw, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    if re.fullmatch(r"[a-z0-9-]{8,}", raw, re.IGNORECASE):
        return raw.lower()
    raise ValueError("Paste a Ballchasing replay id or replay URL.")


def parse_source_entries(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = [str(item or "").strip() for item in value]
    else:
        raw_items = [item.strip() for item in re.split(r"[\n,\r\t]+", str(value or ""))]
    return [item for item in raw_items if item]


def parse_source_inputs(group_text: str | list[str] | tuple[str, ...] | None, creator_text: str | list[str] | tuple[str, ...] | None) -> dict[str, list[str]]:
    groups: list[str] = []
    creators: list[str] = []
    invalid: list[str] = []
    for value in [*parse_source_entries(group_text), *parse_source_entries(creator_text)]:
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc and "ballchasing.com" not in parsed.netloc.lower():
            invalid.append(value)
            continue
        resolved = resolve_ballchasing_source(value)
        if not resolved:
            invalid.append(value)
            continue
        kind, normalized = resolved
        if kind == "group" and normalized not in groups:
            groups.append(normalized)
        elif kind == "creator" and normalized not in creators:
            creators.append(normalized)
    return {"groups": groups, "creators": creators, "invalid": invalid}


def sanitize_platform_config(data: dict[str, Any] | None) -> dict[str, Any]:
    source_data = data or {}
    groups = []
    for value in source_data.get("group_ids") or []:
        normalized = normalize_ballchasing_group_id(str(value or ""))
        if normalized and normalized not in groups:
            groups.append(normalized)
    creators = []
    for value in source_data.get("creator_ids") or []:
        normalized = normalize_ballchasing_creator_id(str(value or ""))
        if normalized and normalized not in creators:
            creators.append(normalized)
    return {
        "ballchasing_api_token": str(source_data.get("ballchasing_api_token") or "").strip(),
        "group_ids": groups,
        "creator_ids": creators,
        "auto_sync_enabled": bool(source_data.get("auto_sync_enabled", DEFAULT_CONFIG["auto_sync_enabled"])),
        "auto_sync_minutes": max(5, min(1440, int(source_data.get("auto_sync_minutes") or DEFAULT_CONFIG["auto_sync_minutes"]))),
        "sync_count": max(1, min(200, int(source_data.get("sync_count") or DEFAULT_CONFIG["sync_count"]))),
        "creator_group_limit": max(1, min(200, int(source_data.get("creator_group_limit") or DEFAULT_CONFIG["creator_group_limit"]))),
    }


def load_platform_config() -> dict[str, Any]:
    paths = ensure_platform_dirs()
    config_file = paths["config_file"]
    if not config_file.exists():
        config = sanitize_platform_config(DEFAULT_CONFIG)
        save_platform_config(config)
        return config
    try:
        raw = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = DEFAULT_CONFIG
    config = sanitize_platform_config(raw)
    if config != raw:
        save_platform_config(config)
    return config


def save_platform_config(config: dict[str, Any]) -> dict[str, Any]:
    paths = ensure_platform_dirs()
    normalized = sanitize_platform_config(config)
    payload = json.dumps(normalized, indent=2, sort_keys=True)
    temp_path = paths["config_file"].with_suffix(".tmp")
    temp_path.write_text(payload, encoding="utf-8")
    temp_path.replace(paths["config_file"])
    return normalized


def public_platform_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "token_saved": bool(str(config.get("ballchasing_api_token") or "").strip()),
        "group_ids": list(config.get("group_ids") or []),
        "creator_ids": list(config.get("creator_ids") or []),
        "group_sources_text": "\n".join(config.get("group_ids") or []),
        "creator_sources_text": "\n".join(config.get("creator_ids") or []),
        "auto_sync_enabled": bool(config.get("auto_sync_enabled")),
        "auto_sync_minutes": int(config.get("auto_sync_minutes") or DEFAULT_CONFIG["auto_sync_minutes"]),
        "sync_count": int(config.get("sync_count") or DEFAULT_CONFIG["sync_count"]),
        "creator_group_limit": int(config.get("creator_group_limit") or DEFAULT_CONFIG["creator_group_limit"]),
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_count(con: duckdb.DuckDBPyConnection, table_name: str, where: str | None = None) -> int:
    if not table_exists(con, table_name):
        return 0
    sql = f"SELECT COUNT(*) FROM {table_name}"
    if where:
        sql = f"{sql} WHERE {where}"
    row = con.execute(sql).fetchone()
    return int(row[0] or 0) if row else 0


def platform_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    return {
        "remote_replays": _safe_count(con, "remote_replays"),
        "parsed_completed": _safe_count(con, "replay_parsed_status", "status = 'completed'"),
        "parsed_failed": _safe_count(con, "replay_parsed_status", "status = 'failed'"),
        "review_cached": _safe_count(con, "replay_review_cache"),
        "local_indexed": _safe_count(con, "local_replay_index"),
    }


def strip_video_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"videos", "video_links", "featured_video", "youtube_videos"}:
                continue
            cleaned[key] = strip_video_metadata(item)
        return cleaned
    if isinstance(value, list):
        return [strip_video_metadata(item) for item in value]
    return value


def snapshot_sync_state() -> dict[str, Any]:
    with STATE_LOCK:
        return json.loads(json.dumps(SYNC_STATE))


def update_sync_state(**updates: Any) -> None:
    with STATE_LOCK:
        SYNC_STATE.update(updates)


@contextmanager
def settings_override(
    *,
    serving_db: Path,
    replay_download_dir: Path,
    ballchasing_api_token: str | None = None,
    group_ids: list[str] | tuple[str, ...] | None = None,
    creator_ids: list[str] | tuple[str, ...] | None = None,
    creator_group_limit: int | None = None,
) -> Iterator[None]:
    keys = {
        "REPLAYOS_SERVING_DB": str(serving_db),
        "REPLAYOS_REPLAY_DOWNLOAD_DIR": str(replay_download_dir),
        "BALLCHASING_API_TOKEN": str(ballchasing_api_token or ""),
        "BALLCHASING_GROUP_IDS": ",".join(group_ids or []),
        "BALLCHASING_CREATOR_IDS": ",".join(creator_ids or []),
        "BALLCHASING_CREATOR_GROUP_LIMIT": str(int(creator_group_limit or DEFAULT_CONFIG["creator_group_limit"])),
    }
    previous: dict[str, str | None] = {key: os.environ.get(key) for key in keys}
    try:
        for key, value in keys.items():
            os.environ[key] = value
        get_settings.cache_clear()
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()

@contextmanager
def workspace_connection(
    *,
    token_override: str | None = None,
    read_only: bool = False,
) -> Iterator[tuple[dict[str, Path], dict[str, Any], duckdb.DuckDBPyConnection]]:
    with WORKSPACE_LOCK:
        paths = ensure_platform_dirs()
        config = load_platform_config()
        token = str(token_override).strip() if token_override is not None else str(config.get("ballchasing_api_token") or "").strip()
        with settings_override(
            serving_db=paths["serving_db"],
            replay_download_dir=paths["download_dir"],
            ballchasing_api_token=token,
            group_ids=config.get("group_ids") or [],
            creator_ids=config.get("creator_ids") or [],
            creator_group_limit=int(config.get("creator_group_limit") or DEFAULT_CONFIG["creator_group_limit"]),
        ):
            con = duckdb.connect(str(paths["serving_db"]), read_only=read_only)
            try:
                yield paths, config, con
            finally:
                con.close()


def platform_status_payload() -> dict[str, Any]:
    with workspace_connection(read_only=False) as (paths, config, con):
        counts = platform_counts(con)
    return {
        "ok": True,
        "title": APP_TITLE,
        "workspace_dir": str(paths["root"]),
        "config": public_platform_config(config),
        "counts": counts,
        "sync": snapshot_sync_state(),
    }


def update_platform_config(payload: ConfigUpdateRequest) -> dict[str, Any]:
    with WORKSPACE_LOCK:
        config = load_platform_config()
        parsed = parse_source_inputs(payload.group_sources_text, payload.creator_sources_text)
        if parsed["invalid"]:
            joined = ", ".join(parsed["invalid"][:5])
            raise ReviewPlatformError(f"Unrecognized Ballchasing source values: {joined}")
        if payload.clear_ballchasing_api_token:
            config["ballchasing_api_token"] = ""
        elif payload.ballchasing_api_token is not None and str(payload.ballchasing_api_token).strip():
            config["ballchasing_api_token"] = str(payload.ballchasing_api_token).strip()
        config["group_ids"] = parsed["groups"]
        config["creator_ids"] = parsed["creators"]
        config["auto_sync_enabled"] = bool(payload.auto_sync_enabled)
        config["auto_sync_minutes"] = int(payload.auto_sync_minutes)
        config["sync_count"] = int(payload.sync_count)
        config["creator_group_limit"] = int(payload.creator_group_limit)
        saved = save_platform_config(config)
    return public_platform_config(saved)


def local_review_payload(
    replay_id: str,
    *,
    replay_token: str | None,
    force_refresh: bool,
) -> dict[str, Any]:
    with workspace_connection(token_override=replay_token or None, read_only=False) as (paths, config, con):
        token = str(replay_token).strip() if replay_token is not None else str(config.get("ballchasing_api_token") or "").strip()
        replay = get_library_replay(con, replay_id)
        has_local_copy = bool(replay and replay.get("local_file_path") and Path(str(replay["local_file_path"])).exists())
        if not token and not has_local_copy:
            raise ReviewPlatformError("Save a Ballchasing API token first, or import a replay that already exists in the local library.")

        if token:
            try:
                ensure_ballchasing_replay_download(
                    replay_id,
                    serving_db=paths["serving_db"],
                    force_download=force_refresh,
                    parse_download=True,
                )
            except BallchasingError as exc:
                raise ReviewPlatformError(str(exc)) from exc
            except ReplayParseError as exc:
                raise ReviewPlatformError(str(exc)) from exc

        refresh_local_replay_index(serving_db=paths["serving_db"], replay_roots=[paths["download_dir"]])
        viewer = replay_viewer(con, replay_id)
        replay = get_library_replay(con, replay_id)
        if viewer is None or replay is None:
            raise ReviewPlatformError("Replay review is unavailable in the local review platform.")
        viewer = strip_video_metadata(viewer)
        replay = strip_video_metadata(replay)
        counts = platform_counts(con)

    return {
        "replay_id": replay_id,
        "prepared_at": now_iso(),
        "workspace_dir": str(paths["root"]),
        "viewer": viewer,
        "replay": replay,
        "counts": counts,
        "native_viewer_url": f"/native-viewer/index.html?replayId={replay_id}&apiBase=",
        "file_url": f"/api/replays/{replay_id}/file",
        "json_url": f"/api/replays/{replay_id}/viewer",
    }


def library_page_payload(
    *,
    limit: int,
    offset: int,
    search: str | None,
    parsed_only: bool,
    review_ready: bool,
    sort: str,
) -> dict[str, Any]:
    with workspace_connection(read_only=False) as (_, _, con):
        payload = library_replay_page(
            con,
            limit=limit,
            offset=offset,
            search=search,
            parsed_only=parsed_only,
            review_ready=review_ready,
            sort_mode=sort,
        )
    return strip_video_metadata(payload)


def run_source_sync(*, trigger: str, count: int | None = None, force_download: bool = False) -> dict[str, Any]:
    started_at = now_iso()
    update_sync_state(running=True, last_started_at=started_at, last_trigger=trigger, last_error=None)
    try:
        with workspace_connection(read_only=False) as (paths, config, con):
            token = str(config.get("ballchasing_api_token") or "").strip()
            if not token:
                raise ReviewPlatformError("Save a Ballchasing API token before syncing creator or group sources.")
            group_ids = list(config.get("group_ids") or [])
            creator_ids = list(config.get("creator_ids") or [])
            if not (group_ids or creator_ids):
                raise ReviewPlatformError("Add at least one Ballchasing group or creator source before syncing.")
            result = sync_ballchasing_source_set(
                paths["serving_db"],
                group_ids=group_ids,
                creator_ids=creator_ids,
                creator_group_limit=int(config.get("creator_group_limit") or DEFAULT_CONFIG["creator_group_limit"]),
                count=int(count or config.get("sync_count") or DEFAULT_CONFIG["sync_count"]),
                download_files=True,
                fetch_details=True,
                force_download=force_download,
                parse_downloads=True,
            )
            index_result = refresh_local_replay_index(serving_db=paths["serving_db"], replay_roots=[paths["download_dir"]])
            counts = platform_counts(con)
        payload = {"sync": result, "index": index_result, "counts": counts}
        update_sync_state(running=False, last_completed_at=now_iso(), last_result=payload, last_error=None)
        return payload
    except Exception as exc:  # noqa: BLE001
        update_sync_state(running=False, last_completed_at=now_iso(), last_error=str(exc))
        raise


def auto_sync_worker() -> None:
    while not AUTO_SYNC_STOP.wait(15):
        try:
            config = load_platform_config()
            if not config.get("auto_sync_enabled"):
                continue
            last_completed_raw = snapshot_sync_state().get("last_completed_at")
            last_completed = None
            if last_completed_raw:
                try:
                    last_completed = datetime.fromisoformat(str(last_completed_raw).replace("Z", "+00:00"))
                except ValueError:
                    last_completed = None
            interval_seconds = max(300, int(config.get("auto_sync_minutes") or DEFAULT_CONFIG["auto_sync_minutes"]) * 60)
            if snapshot_sync_state().get("running"):
                continue
            if last_completed is not None:
                elapsed = (datetime.now(timezone.utc) - last_completed).total_seconds()
                if elapsed < interval_seconds:
                    continue
            run_source_sync(trigger="auto")
        except Exception as exc:  # noqa: BLE001
            update_sync_state(running=False, last_completed_at=now_iso(), last_error=str(exc))


@app.on_event("startup")
def startup() -> None:
    global AUTO_SYNC_THREAD
    ensure_platform_dirs()
    if AUTO_SYNC_THREAD is None or not AUTO_SYNC_THREAD.is_alive():
        AUTO_SYNC_STOP.clear()
        AUTO_SYNC_THREAD = threading.Thread(target=auto_sync_worker, name="review-platform-auto-sync", daemon=True)
        AUTO_SYNC_THREAD.start()


@app.on_event("shutdown")
def shutdown() -> None:
    AUTO_SYNC_STOP.set()


def homepage_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ReplayOS Review Platform</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0d0f;
      --panel: #121518;
      --panel-2: #191d21;
      --line: rgba(255,255,255,0.1);
      --text: #f4efe6;
      --muted: #b6b9bd;
      --teal: #20b3aa;
      --gold: #f0c76a;
      --red: #df6e63;
      --blue: #18b4d9;
      --orange: #f59b54;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: linear-gradient(180deg, #0b0d0f, #101317);
      color: var(--text);
      font-family: Inter, system-ui, sans-serif;
    }
    .shell {
      margin: 0 auto;
      max-width: 1680px;
      padding: 24px;
      display: grid;
      gap: 18px;
    }
    .hero, .panel {
      background: rgba(255,255,255,0.03);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }
    .hero {
      display: grid;
      gap: 14px;
    }
    h1, h2, h3, p { margin: 0; }
    .hero h1 { font-size: clamp(2rem, 4vw, 3rem); line-height: 1; }
    .hero p, .meta, .note, .empty-copy { color: var(--muted); }
    .kicker {
      color: var(--gold);
      font-size: 0.8rem;
      font-weight: 800;
      text-transform: uppercase;
    }
    .state-strip {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(5, minmax(0, 1fr));
    }
    .state-card, .summary-card, .library-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .state-card, .summary-card {
      display: grid;
      gap: 6px;
      min-height: 96px;
    }
    .state-card strong, .summary-card strong {
      font-size: 1.45rem;
      line-height: 1;
    }
    .app-grid {
      display: grid;
      gap: 18px;
      grid-template-columns: 360px minmax(0, 1fr);
      align-items: start;
    }
    .sidebar, .content {
      display: grid;
      gap: 18px;
    }
    .panel-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    form, .stack {
      display: grid;
      gap: 12px;
    }
    label {
      display: grid;
      gap: 6px;
      font-size: 0.92rem;
    }
    input[type="text"],
    input[type="password"],
    input[type="number"],
    textarea,
    select {
      width: 100%;
      background: #0c0f12;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--text);
      padding: 10px 12px;
      font: inherit;
    }
    textarea {
      min-height: 88px;
      resize: vertical;
    }
    .inline-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .check {
      align-items: center;
      display: inline-flex;
      gap: 8px;
      font-size: 0.9rem;
      color: var(--muted);
    }
    .actions, .toolbar, .filter-row, .library-footer {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    button, .link-button {
      appearance: none;
      background: var(--teal);
      border: 1px solid transparent;
      border-radius: 8px;
      color: #061011;
      cursor: pointer;
      font: inherit;
      font-weight: 800;
      padding: 10px 14px;
      text-decoration: none;
    }
    button.secondary, .link-button.secondary {
      background: transparent;
      border-color: var(--line);
      color: var(--text);
    }
    button:disabled {
      cursor: wait;
      opacity: 0.6;
    }
    .status-line {
      min-height: 1.4rem;
      color: var(--muted);
      font-size: 0.92rem;
    }
    .status-line.error { color: #ff9f95; }
    .library-list {
      display: grid;
      gap: 10px;
      max-height: 54rem;
      overflow: auto;
      padding-right: 4px;
    }
    .library-card {
      cursor: pointer;
      display: grid;
      gap: 8px;
    }
    .library-card.selected {
      border-color: rgba(32, 179, 170, 0.8);
      box-shadow: 0 0 0 1px rgba(32, 179, 170, 0.22) inset;
    }
    .library-title-row, .library-meta-row, .library-review-row {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: start;
    }
    .library-title-row strong {
      font-size: 1rem;
      line-height: 1.2;
    }
    .tag {
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      font-size: 0.78rem;
      padding: 3px 8px;
      white-space: nowrap;
    }
    .tag.good { color: #96ffd3; }
    .tag.warn { color: #ffd38d; }
    .tag.bad { color: #ff9f95; }
    .review-shell {
      display: grid;
      gap: 18px;
    }
    .viewer-shell {
      display: grid;
      gap: 14px;
    }
    .summary-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    .edge-wrap, .viewer-panel, .detail-grid, .boxscores {
      display: grid;
      gap: 14px;
    }
    .edge-bar {
      position: relative;
      display: grid;
      gap: 2px;
      grid-template-columns: repeat(42, minmax(0, 1fr));
      min-height: 72px;
      background: #0a0d10;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      padding: 6px;
    }
    .edge-segment { border-radius: 2px; }
    .edge-labels, .legend {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 0.88rem;
    }
    .legend {
      justify-content: flex-start;
    }
    .legend-chip {
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      display: inline-flex;
      gap: 8px;
      padding: 5px 10px;
    }
    iframe {
      width: 100%;
      min-height: 760px;
      background: #040608;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .detail-grid {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .stack {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .stack-list {
      display: grid;
      gap: 8px;
    }
    .stack-row {
      display: grid;
      grid-template-columns: minmax(72px, auto) minmax(0, 1fr);
      gap: 8px 12px;
      align-items: start;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
    }
    .boxscores {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .boxscore-side {
      display: grid;
      gap: 10px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .boxscore-row {
      display: grid;
      gap: 6px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-2);
    }
    .empty-state {
      display: grid;
      place-items: center;
      min-height: 18rem;
      text-align: center;
      padding: 24px;
    }
    @media (max-width: 1280px) {
      .summary-grid, .detail-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .state-strip { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 980px) {
      .app-grid, .summary-grid, .detail-grid, .boxscores, .inline-grid, .state-strip { grid-template-columns: 1fr; }
      iframe { min-height: 560px; }
      .library-list { max-height: none; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div>
        <p class="kicker">Review-Only ReplayOS</p>
        <h1>Run the simplified replay review app.</h1>
      </div>
      <p>This keeps the ReplayOS review core: replay semantics, team recognition, player naming, series grouping, and the native 60 Hz viewer. It leaves out the bigger site modules like records, live coverage, and broader pro-data pages.</p>
      <div id="state-strip" class="state-strip"></div>
    </section>

    <div class="app-grid">
      <aside class="sidebar">
        <section class="panel">
          <div class="panel-head">
            <div>
              <p class="kicker">Ballchasing</p>
              <h2>Source settings</h2>
            </div>
          </div>
          <form id="config-form">
            <label>
              Save Ballchasing API token
              <input id="config-token" type="password" placeholder="Leave blank to keep the saved token">
            </label>
            <label class="check">
              <input id="config-clear-token" type="checkbox">
              Clear saved token
            </label>
            <label>
              Group sources
              <textarea id="config-groups" placeholder="One Ballchasing group id or URL per line"></textarea>
            </label>
            <label>
              Creator sources
              <textarea id="config-creators" placeholder="One Ballchasing creator id or URL per line"></textarea>
            </label>
            <div class="inline-grid">
              <label>
                Sync count
                <input id="config-sync-count" type="number" min="1" max="200" value="8">
              </label>
              <label>
                Creator group limit
                <input id="config-creator-limit" type="number" min="1" max="200" value="12">
              </label>
            </div>
            <div class="inline-grid">
              <label class="check">
                <input id="config-auto-sync" type="checkbox">
                Auto-sync configured sources
              </label>
              <label>
                Auto-sync minutes
                <input id="config-auto-minutes" type="number" min="5" max="1440" value="30">
              </label>
            </div>
            <div class="actions">
              <button type="submit">Save settings</button>
              <button id="sync-now" type="button" class="secondary">Sync sources now</button>
            </div>
          </form>
          <div id="config-status" class="status-line">Load the local review workspace, then save your sources.</div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div>
              <p class="kicker">Import</p>
              <h2>Add one replay</h2>
            </div>
          </div>
          <form id="import-form">
            <label>
              Ballchasing replay URL or replay id
              <input id="import-replay" type="text" placeholder="https://ballchasing.com/replay/...">
            </label>
            <label class="check">
              <input id="import-force" type="checkbox">
              Force re-download and re-parse
            </label>
            <div class="actions">
              <button type="submit">Add replay</button>
            </div>
          </form>
          <div id="import-status" class="status-line">Single replay imports use the saved token unless you clear it.</div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div>
              <p class="kicker">Replay shelf</p>
              <h2>Library</h2>
            </div>
          </div>
          <div class="stack">
            <label>
              Search
              <input id="library-search" type="text" placeholder="Team, player, replay id">
            </label>
            <div class="inline-grid">
              <label class="check">
                <input id="library-parsed-only" type="checkbox">
                Parsed only
              </label>
              <label class="check">
                <input id="library-review-ready" type="checkbox">
                Review ready
              </label>
            </div>
            <div class="filter-row">
              <label style="min-width: 11rem;">
                Sort
                <select id="library-sort">
                  <option value="recent">Recent</option>
                  <option value="series">Series</option>
                </select>
              </label>
              <button id="library-refresh" type="button" class="secondary">Refresh</button>
            </div>
          </div>
          <div id="library-status" class="status-line">Loading library...</div>
          <div id="library-list" class="library-list"></div>
          <div class="library-footer">
            <button id="library-load-more" type="button" class="secondary" style="display:none;">Load more</button>
          </div>
        </section>
      </aside>

      <section class="content">
        <section id="review-shell" class="review-shell">
          <div id="viewer-empty" class="panel empty-state">
            <div class="empty-copy">
              <h2>Pick a replay from the shelf.</h2>
              <p>Import one replay or sync a creator/group source, then open the review stack here.</p>
            </div>
          </div>

          <div id="viewer-content" class="viewer-shell" style="display:none;">
            <div class="toolbar">
              <a id="open-native" class="link-button" href="#" target="_blank" rel="noreferrer">Open full 3D viewer</a>
              <a id="download-replay" class="link-button secondary" href="#" target="_blank" rel="noreferrer">Download local replay</a>
              <a id="json-link" class="link-button secondary" href="#" target="_blank" rel="noreferrer">Open review JSON</a>
            </div>

            <div id="summary" class="summary-grid"></div>

            <section class="panel edge-wrap">
              <div>
                <p class="kicker">Win edge</p>
                <h2 id="viewer-title">Replay review</h2>
                <p id="viewer-meta" class="meta"></p>
              </div>
              <div id="edge-bar" class="edge-bar"></div>
              <div class="edge-labels">
                <span>Orange edge</span>
                <span>Blue edge</span>
              </div>
              <div class="legend">
                <span class="legend-chip"><strong>Vol</strong><span>Total movement in win probability.</span></span>
                <span class="legend-chip"><strong>TO</strong><span>Turnover swing.</span></span>
                <span class="legend-chip"><strong>PR</strong><span>Pressure swing.</span></span>
                <span class="legend-chip"><strong>G</strong><span>Goal swing.</span></span>
              </div>
            </section>

            <section class="panel viewer-panel">
              <div>
                <p class="kicker">Native 3D Viewer</p>
                <h2>Replay cameras and playback</h2>
              </div>
              <iframe id="native-frame" title="ReplayOS review platform native viewer" allow="fullscreen; autoplay; clipboard-write"></iframe>
            </section>

            <div class="detail-grid">
              <section class="stack">
                <h3>Turning points</h3>
                <div id="turning-points" class="stack-list"></div>
              </section>
              <section class="stack">
                <h3>Blunders</h3>
                <div id="blunders" class="stack-list"></div>
              </section>
              <section class="stack">
                <h3>Best plays</h3>
                <div id="plays" class="stack-list"></div>
              </section>
              <section class="stack">
                <h3>Clutch plays</h3>
                <div id="clutch-plays" class="stack-list"></div>
              </section>
              <section class="stack">
                <h3>Player impact</h3>
                <div id="player-impact" class="stack-list"></div>
              </section>
              <section class="stack">
                <h3>Model reasons</h3>
                <div id="model-reasons" class="stack-list"></div>
              </section>
            </div>

            <div class="boxscores">
              <section class="boxscore-side">
                <h3 id="blue-box-title">Blue</h3>
                <div id="blue-box"></div>
              </section>
              <section class="boxscore-side">
                <h3 id="orange-box-title">Orange</h3>
                <div id="orange-box"></div>
              </section>
            </div>
          </div>
        </section>
      </section>
    </div>
  </main>

  <script>
    const state = {
      configHydrated: false,
      selectedReplayId: null,
      libraryOffset: 0,
      libraryLimit: 40,
      libraryHasMore: false,
    };

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
    }

    function fmtNumber(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
      return Number(value).toFixed(digits);
    }

    function fmtPercent(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
      return `${(Number(value) * 100).toFixed(1)}%`;
    }

    function fmtSwingPoints(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
      const numeric = Number(value);
      const prefix = numeric > 0 ? '+' : '';
      return `${prefix}${numeric.toFixed(1)} pts`;
    }

    function shortText(value, max = 44) {
      if (!value) return 'Untitled';
      if (value.length <= max) return value;
      return `${value.slice(0, max - 3)}...`;
    }

    function reviewNote(review) {
      if (!review) return 'Review pending';
      if (review.largest_blunder?.player_name) return `${shortText(review.largest_blunder.player_name, 18)} blunder`;
      if (review.best_play?.player_name) return `${shortText(review.best_play.player_name, 18)} big play`;
      if (review.turning_point?.label) return shortText(review.turning_point.label, 28);
      return 'Low-swing replay';
    }

    function formatDate(value) {
      if (!value) return 'Date pending';
      const date = new Date(typeof value === 'number' ? value * 1000 : value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
    }

    function formatAgo(value) {
      if (!value) return 'not yet';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
      if (seconds < 60) return `${seconds}s ago`;
      if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
      if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
      return `${Math.round(seconds / 86400)}d ago`;
    }

    async function fetchJson(path, options = {}) {
      const response = await fetch(path, {
        cache: 'no-store',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          ...(options.headers || {}),
        },
        ...options,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || payload.error || `Request failed: ${response.status}`);
      }
      return payload;
    }

    function setStatus(id, message, tone = 'note') {
      const node = document.getElementById(id);
      node.textContent = message;
      node.className = `status-line ${tone === 'error' ? 'error' : ''}`;
    }

    function hydrateConfigForm(config) {
      document.getElementById('config-groups').value = config.group_sources_text || '';
      document.getElementById('config-creators').value = config.creator_sources_text || '';
      document.getElementById('config-auto-sync').checked = Boolean(config.auto_sync_enabled);
      document.getElementById('config-auto-minutes').value = config.auto_sync_minutes || 30;
      document.getElementById('config-sync-count').value = config.sync_count || 8;
      document.getElementById('config-creator-limit').value = config.creator_group_limit || 12;
      document.getElementById('config-token').placeholder = config.token_saved ? 'Saved locally. Enter a new token to replace it.' : 'Paste Ballchasing token here';
      state.configHydrated = true;
    }

    function renderStateStrip(status) {
      const counts = status.counts || {};
      const sync = status.sync || {};
      const config = status.config || {};
      const cards = [
        ['Library', counts.remote_replays ?? 0, `${counts.review_cached ?? 0} review-ready`],
        ['Parses', counts.parsed_completed ?? 0, `${counts.parsed_failed ?? 0} failed`],
        ['Sources', `${(config.group_ids || []).length} groups`, `${(config.creator_ids || []).length} creators`],
        ['Token', config.token_saved ? 'saved' : 'missing', config.token_saved ? 'ready for imports and sync' : 'save Ballchasing token'],
        ['Sync', sync.running ? 'running' : 'idle', sync.last_completed_at ? `last ${formatAgo(sync.last_completed_at)}` : 'no sync run yet'],
      ];
      document.getElementById('state-strip').innerHTML = cards.map(([label, value, note]) => `
        <article class="state-card">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
          <em class="meta">${escapeHtml(note)}</em>
        </article>
      `).join('');
    }

    function renderLibrary(payload, append = false) {
      const listNode = document.getElementById('library-list');
      const items = payload.items || [];
      const html = items.map((item) => {
        const selected = String(item.replay_id) === String(state.selectedReplayId);
        const review = item.review || null;
        const parsedStatus = String(item.carball_status?.status || '').toLowerCase();
        const parseTag = parsedStatus === 'completed'
          ? '<span class="tag good">parsed</span>'
          : parsedStatus === 'failed'
            ? '<span class="tag bad">parse failed</span>'
            : '<span class="tag warn">pending</span>';
        const seriesTag = item.series_name ? `<span class="tag">${escapeHtml(item.series_name)}</span>` : '';
        return `
          <article class="library-card ${selected ? 'selected' : ''}" data-replay-id="${escapeHtml(item.replay_id)}">
            <div class="library-title-row">
              <strong>${escapeHtml(shortText(item.title || `${item.blue_team_name || 'Blue'} vs ${item.orange_team_name || 'Orange'}`, 54))}</strong>
              ${seriesTag}
            </div>
            <div class="library-meta-row meta">
              <span>${escapeHtml(formatDate(item.match_date))}</span>
              <span>${escapeHtml(item.blue_goals ?? '?')} - ${escapeHtml(item.orange_goals ?? '?')}</span>
            </div>
            <div class="library-review-row">
              <div class="meta">
                <div>${escapeHtml(item.blue_team_name || 'Blue')} vs ${escapeHtml(item.orange_team_name || 'Orange')}</div>
                <div>${review ? `Vol ${fmtNumber(review.volatility, 2)} | ${review.swing_count || 0} swings` : reviewNote(null)}</div>
                <div>${escapeHtml(review ? reviewNote(review) : 'Open the replay to build the full review view.')}</div>
              </div>
              ${parseTag}
            </div>
          </article>
        `;
      }).join('');
      if (append) listNode.insertAdjacentHTML('beforeend', html);
      else listNode.innerHTML = html || '<div class="empty-copy">No replays yet. Import one replay or sync a source.</div>';
      state.libraryHasMore = Boolean(payload.has_more);
      document.getElementById('library-load-more').style.display = state.libraryHasMore ? 'inline-flex' : 'none';
      setStatus('library-status', `${payload.total || 0} replays in the local review shelf.`);
      listNode.querySelectorAll('.library-card').forEach((card) => {
        card.addEventListener('click', () => {
          const replayId = card.getAttribute('data-replay-id');
          if (replayId) selectReplay(replayId);
        });
      });
    }

    function buildSummary(viewer) {
      const replay = viewer.replay || {};
      const evalData = viewer.eval || {};
      const impact = viewer.player_impact || [];
      const cards = [
        ['Blue win', fmtPercent(evalData.final_blue_probability), `Start ${fmtPercent(evalData.base_blue_probability)}`],
        ['Orange win', evalData.final_blue_probability === null || evalData.final_blue_probability === undefined ? 'n/a' : fmtPercent(1 - Number(evalData.final_blue_probability)), 'Live edge from replay events'],
        ['Volatility', fmtSwingPoints(evalData.volatility_points), `${(evalData.plays || []).length + (evalData.blunders || []).length} major swings`],
        ['Swing count', fmtNumber(evalData.swing_count, 0), evalData.largest_swing ? `${fmtSwingPoints(evalData.largest_swing.swing_points)} at ${fmtNumber(evalData.largest_swing.t, 1)}s` : 'No swing registered'],
        ['Largest blunder', (evalData.blunders || [])[0]?.player_name || 'None yet', (evalData.blunders || [])[0] ? `${fmtSwingPoints(evalData.blunders[0].swing_points)} at ${fmtNumber(evalData.blunders[0].t, 1)}s` : 'No major blunder flagged'],
        ['Clutch play', (evalData.clutch_plays || [])[0]?.player_name || 'None yet', (evalData.clutch_plays || [])[0] ? `${fmtSwingPoints(evalData.clutch_plays[0].swing_points)} at ${fmtNumber(evalData.clutch_plays[0].t, 1)}s` : 'No late-game dagger flagged'],
        ['Impact leader', impact[0]?.player_name || 'None yet', impact[0] ? `${impact[0].goals || 0} G, ${impact[0].positive_swings || 0} positive swings` : 'Waiting for impact breakdown'],
        ['Scoreline', `${replay.blue_team_name || 'Blue'} ${replay.blue_goals ?? 0} - ${replay.orange_goals ?? 0} ${replay.orange_team_name || 'Orange'}`, replay.map_code || 'Map pending'],
      ];
      return cards.map(([label, value, note]) => `<article class="summary-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><em class="meta">${escapeHtml(note)}</em></article>`).join('');
    }

    function buildEdgeBar(edge) {
      const segments = edge?.segments || [];
      return segments.map((segment) => {
        const intensity = Math.abs(Number(segment.blue_edge || 0)) * 1.9 + 0.18;
        const color = Number(segment.blue_edge || 0) >= 0
          ? `rgba(19, 168, 154, ${Math.min(1, intensity)})`
          : `rgba(217, 86, 77, ${Math.min(1, intensity)})`;
        return `<span class="edge-segment" title="${escapeHtml(`${segment.end_t}s | Blue ${fmtPercent(segment.blue_probability)}`)}" style="background:${color}"></span>`;
      }).join('');
    }

    function stackRows(rows, formatter) {
      if (!rows?.length) return '<div class="stack-row"><strong>No data yet.</strong></div>';
      return rows.map((row) => formatter(row)).join('');
    }

    function fillBoxscores(players, blueName, orangeName) {
      const blue = [];
      const orange = [];
      for (const player of players || []) {
        const side = String(player.side || '').toLowerCase();
        if (side === 'orange') orange.push(player);
        else blue.push(player);
      }
      document.getElementById('blue-box-title').textContent = blueName || 'Blue';
      document.getElementById('orange-box-title').textContent = orangeName || 'Orange';
      document.getElementById('blue-box').innerHTML = stackRows(blue, (player) => `<div class="boxscore-row"><strong>${escapeHtml(player.player_name || 'Unknown')}</strong><span>${escapeHtml(player.car_name || player.car_family || 'Body pending')}</span><span>${player.goals ?? 0} G, ${player.assists ?? 0} A, ${player.saves ?? 0} S, ${player.score ?? '-'} score</span></div>`);
      document.getElementById('orange-box').innerHTML = stackRows(orange, (player) => `<div class="boxscore-row"><strong>${escapeHtml(player.player_name || 'Unknown')}</strong><span>${escapeHtml(player.car_name || player.car_family || 'Body pending')}</span><span>${player.goals ?? 0} G, ${player.assists ?? 0} A, ${player.saves ?? 0} S, ${player.score ?? '-'} score</span></div>`);
    }

    function renderReview(payload) {
      const viewer = payload.viewer || {};
      const replay = viewer.replay || {};
      const evalData = viewer.eval || {};
      const prediction = (viewer.predictions || [])[0] || {};
      state.selectedReplayId = payload.replay_id;
      document.getElementById('viewer-empty').style.display = 'none';
      document.getElementById('viewer-content').style.display = 'grid';
      document.getElementById('summary').innerHTML = buildSummary(viewer);
      document.getElementById('edge-bar').innerHTML = buildEdgeBar(viewer.win_edge || {});
      document.getElementById('viewer-title').textContent = replay.title || payload.replay_id;
      document.getElementById('viewer-meta').textContent = `${replay.blue_team_name || 'Blue'} vs ${replay.orange_team_name || 'Orange'} | ${replay.map_code || 'Map pending'} | 60 Hz native viewer`;
      document.getElementById('native-frame').src = `${payload.native_viewer_url}${encodeURIComponent(window.location.origin)}`;
      document.getElementById('open-native').href = document.getElementById('native-frame').src;
      document.getElementById('download-replay').href = payload.file_url;
      document.getElementById('json-link').href = payload.json_url;
      document.getElementById('turning-points').innerHTML = stackRows((viewer.timeline?.turning_points || []).slice(0, 8), (row) => `<div class="stack-row"><span>${fmtNumber(row.t, 1)}s</span><div><strong>${escapeHtml(row.label || row.event_type || 'Moment')}</strong><em class="meta">${escapeHtml(row.event_type || 'event')}</em></div></div>`);
      document.getElementById('blunders').innerHTML = stackRows((evalData.blunders || []).slice(0, 8), (row) => `<div class="stack-row"><span>${fmtNumber(row.t, 1)}s</span><div><strong>${escapeHtml(row.label || 'Blunder')}</strong><em class="meta">${escapeHtml(fmtSwingPoints(row.swing_points))}</em></div></div>`);
      document.getElementById('plays').innerHTML = stackRows((evalData.plays || []).slice(0, 8), (row) => `<div class="stack-row"><span>${fmtNumber(row.t, 1)}s</span><div><strong>${escapeHtml(row.label || 'Play')}</strong><em class="meta">${escapeHtml(fmtSwingPoints(row.swing_points))}</em></div></div>`);
      document.getElementById('clutch-plays').innerHTML = stackRows((evalData.clutch_plays || []).slice(0, 8), (row) => `<div class="stack-row"><span>${fmtNumber(row.t, 1)}s</span><div><strong>${escapeHtml(row.label || 'Clutch play')}</strong><em class="meta">${escapeHtml(fmtSwingPoints(row.swing_points))}</em></div></div>`);
      document.getElementById('player-impact').innerHTML = stackRows((viewer.player_impact || []).slice(0, 8), (row) => `<div class="stack-row"><span>${fmtNumber(row.net_impact, 3)}</span><div><strong>${escapeHtml(row.player_name || 'Player')}</strong><em class="meta">${row.goals || 0} G, ${row.touches || 0} touches, ${row.positive_swings || 0}/${row.negative_swings || 0} swings</em></div></div>`);
      document.getElementById('model-reasons').innerHTML = stackRows((prediction.reasons || []).slice(0, 8), (row) => `<div class="stack-row"><span>${escapeHtml(row.feature || row.name || 'Reason')}</span><div><strong>${escapeHtml(fmtNumber(row.contribution ?? row.value_z ?? 0, 3))}</strong><em class="meta">${escapeHtml(fmtNumber(row.value_z ?? row.value ?? 0, 3))}</em></div></div>`);
      fillBoxscores(replay.players || [], replay.blue_team_name, replay.orange_team_name);
      document.querySelectorAll('.library-card').forEach((card) => {
        card.classList.toggle('selected', card.getAttribute('data-replay-id') === String(state.selectedReplayId));
      });
    }

    async function loadStatus() {
      const status = await fetchJson('/api/status');
      renderStateStrip(status);
      if (!state.configHydrated) hydrateConfigForm(status.config || {});
      const sync = status.sync || {};
      if (sync.running) {
        setStatus('config-status', 'Sync is running in the background.');
      } else if (sync.last_error) {
        setStatus('config-status', sync.last_error, 'error');
      } else if (sync.last_completed_at) {
        const summary = sync.last_result?.sync;
        const note = summary ? `Last sync ${formatAgo(sync.last_completed_at)} | +${summary.inserted || 0} new, ${summary.downloaded || 0} downloaded, ${summary.parsed || 0} parsed` : `Last sync ${formatAgo(sync.last_completed_at)}`;
        setStatus('config-status', note);
      } else {
        setStatus('config-status', status.config?.token_saved ? 'Settings loaded. Save sources, then sync or import replays.' : 'Save a Ballchasing token, then add replay sources.');
      }
    }

    async function loadLibrary(reset = true) {
      const params = new URLSearchParams();
      params.set('limit', String(state.libraryLimit));
      params.set('offset', String(reset ? 0 : state.libraryOffset));
      const search = document.getElementById('library-search').value.trim();
      if (search) params.set('search', search);
      if (document.getElementById('library-parsed-only').checked) params.set('parsed_only', 'true');
      if (document.getElementById('library-review-ready').checked) params.set('review_ready', 'true');
      params.set('sort', document.getElementById('library-sort').value);
      setStatus('library-status', 'Loading library...');
      const payload = await fetchJson(`/api/replays?${params.toString()}`);
      state.libraryOffset = (reset ? 0 : state.libraryOffset) + (payload.items || []).length;
      renderLibrary(payload, !reset);
      if (reset && !state.selectedReplayId && payload.items?.length) {
        await selectReplay(payload.items[0].replay_id);
      }
    }

    async function selectReplay(replayId) {
      setStatus('library-status', `Loading ${replayId}...`);
      const payload = await fetchJson(`/api/replays/${encodeURIComponent(replayId)}/viewer`);
      renderReview({
        replay_id: replayId,
        viewer: payload,
        native_viewer_url: `/native-viewer/index.html?replayId=${encodeURIComponent(replayId)}&apiBase=`,
        file_url: `/api/replays/${encodeURIComponent(replayId)}/file`,
        json_url: `/api/replays/${encodeURIComponent(replayId)}/viewer`,
      });
      setStatus('library-status', `Loaded ${replayId}.`);
    }

    async function saveSettings(event) {
      event.preventDefault();
      setStatus('config-status', 'Saving settings...');
      try {
        const payload = await fetchJson('/api/config', {
          method: 'POST',
          body: JSON.stringify({
            ballchasing_api_token: document.getElementById('config-token').value.trim() || null,
            clear_ballchasing_api_token: document.getElementById('config-clear-token').checked,
            group_sources_text: document.getElementById('config-groups').value,
            creator_sources_text: document.getElementById('config-creators').value,
            auto_sync_enabled: document.getElementById('config-auto-sync').checked,
            auto_sync_minutes: Number(document.getElementById('config-auto-minutes').value || 30),
            sync_count: Number(document.getElementById('config-sync-count').value || 8),
            creator_group_limit: Number(document.getElementById('config-creator-limit').value || 12),
          }),
        });
        document.getElementById('config-token').value = '';
        document.getElementById('config-clear-token').checked = false;
        hydrateConfigForm(payload);
        setStatus('config-status', 'Settings saved.');
        await loadStatus();
      } catch (error) {
        setStatus('config-status', error.message || 'Could not save settings.', 'error');
      }
    }

    async function syncSources(forceDownload = false) {
      setStatus('config-status', 'Syncing configured sources...');
      try {
        const payload = await fetchJson('/api/sources/sync', {
          method: 'POST',
          body: JSON.stringify({
            count: Number(document.getElementById('config-sync-count').value || 8),
            force_download: forceDownload,
          }),
        });
        const summary = payload.sync || {};
        setStatus('config-status', `Sync complete. +${summary.inserted || 0} new, ${summary.downloaded || 0} downloaded, ${summary.parsed || 0} parsed.`);
        state.libraryOffset = 0;
        await Promise.all([loadStatus(), loadLibrary(true)]);
      } catch (error) {
        setStatus('config-status', error.message || 'Source sync failed.', 'error');
      }
    }

    async function importReplay(event) {
      event.preventDefault();
      setStatus('import-status', 'Adding replay...');
      try {
        const payload = await fetchJson('/api/replays/import', {
          method: 'POST',
          body: JSON.stringify({
            replay_input: document.getElementById('import-replay').value.trim(),
            force_refresh: document.getElementById('import-force').checked,
          }),
        });
        document.getElementById('import-replay').value = '';
        document.getElementById('import-force').checked = false;
        setStatus('import-status', `Prepared ${payload.replay_id}.`);
        state.libraryOffset = 0;
        await Promise.all([loadStatus(), loadLibrary(true)]);
        renderReview(payload);
      } catch (error) {
        setStatus('import-status', error.message || 'Replay import failed.', 'error');
      }
    }

    document.getElementById('config-form').addEventListener('submit', saveSettings);
    document.getElementById('sync-now').addEventListener('click', () => syncSources(false));
    document.getElementById('import-form').addEventListener('submit', importReplay);
    document.getElementById('library-refresh').addEventListener('click', () => {
      state.libraryOffset = 0;
      loadLibrary(true).catch((error) => setStatus('library-status', error.message || 'Could not refresh library.', 'error'));
    });
    document.getElementById('library-load-more').addEventListener('click', () => {
      loadLibrary(false).catch((error) => setStatus('library-status', error.message || 'Could not load more replays.', 'error'));
    });

    (async () => {
      try {
        await loadStatus();
        await loadLibrary(true);
      } catch (error) {
        setStatus('library-status', error.message || 'Could not initialize the review platform.', 'error');
      }
    })();

    setInterval(() => {
      if (document.hidden) return;
      loadStatus().catch(() => {});
    }, 15000);
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return homepage_html()


@app.get("/health")
def health() -> dict[str, Any]:
    status = platform_status_payload()
    return {"ok": True, "title": APP_TITLE, "counts": status["counts"]}


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    return platform_status_payload()


@app.get("/api/config")
def api_config() -> dict[str, Any]:
    return public_platform_config(load_platform_config())


@app.post("/api/config")
def api_update_config(payload: ConfigUpdateRequest) -> dict[str, Any]:
    try:
        return update_platform_config(payload)
    except ReviewPlatformError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/replays/import")
def api_import_replay(payload: ReviewRequest) -> dict[str, Any]:
    try:
        replay_id = normalize_replay_id(payload.replay_input)
        return local_review_payload(
            replay_id,
            replay_token=(payload.ballchasing_api_token or "").strip() or None,
            force_refresh=bool(payload.force_refresh),
        )
    except (ValueError, ReviewPlatformError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/review")
def api_prepare_review(payload: ReviewRequest) -> dict[str, Any]:
    return api_import_replay(payload)


@app.post("/api/sources/sync")
def api_sync_sources(payload: SourceSyncRequest) -> dict[str, Any]:
    try:
        return run_source_sync(trigger="manual", count=payload.count, force_download=bool(payload.force_download))
    except ReviewPlatformError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BallchasingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/replays")
def api_replays(
    limit: int = Query(default=40, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = None,
    parsed_only: bool = False,
    review_ready: bool = False,
    sort: str = Query(default="recent"),
) -> dict[str, Any]:
    return library_page_payload(
        limit=limit,
        offset=offset,
        search=search,
        parsed_only=parsed_only,
        review_ready=review_ready,
        sort=sort,
    )


@app.get("/api/replays/{replay_id}")
def api_replay_detail(replay_id: str) -> dict[str, Any]:
    replay_id = normalize_replay_id(replay_id)
    with workspace_connection(read_only=False) as (_, _, con):
        replay = get_library_replay(con, replay_id)
    if replay is None:
        raise HTTPException(status_code=404, detail="Replay not found in the local review shelf.")
    return replay


@app.get("/api/replays/{replay_id}/viewer")
def api_replay_viewer(replay_id: str) -> dict[str, Any]:
    replay_id = normalize_replay_id(replay_id)
    with workspace_connection(read_only=False) as (_, _, con):
        viewer = replay_viewer(con, replay_id)
    if viewer is None:
        raise HTTPException(status_code=404, detail="Replay review is unavailable in the local review shelf.")
    return viewer


@app.get("/api/replays/{replay_id}/file")
def api_replay_file(replay_id: str) -> FileResponse:
    replay_id = normalize_replay_id(replay_id)
    with workspace_connection(read_only=False) as (_, _, con):
        replay = get_library_replay(con, replay_id)
    if replay is None:
        raise HTTPException(status_code=404, detail="Replay not found in the local review shelf.")
    local_file = replay.get("local_file_path")
    if not local_file or not Path(str(local_file)).exists():
        raise HTTPException(status_code=404, detail="Replay file is not downloaded locally.")
    return FileResponse(path=local_file, media_type="application/octet-stream", filename=f"{replay_id}.replay")


@app.get("/api/replays/{replay_id}/native-viewer")
def api_replay_native_viewer(
    request: Request,
    replay_id: str,
    hz: int = Query(default=60, ge=1, le=60),
    max_frames: int = Query(default=24000, ge=300, le=250000),
    start_frame: int = Query(default=0, ge=0, le=250000),
) -> Response:
    replay_id = normalize_replay_id(replay_id)
    cached_payload = load_native_viewer_payload_cache(
        replay_id,
        hz=hz,
        max_frames=max_frames,
        start_frame=start_frame,
        parser_version=PARSER_VERSION,
    )
    if cached_payload:
        return native_viewer_gzip_response(cached_payload, request)

    with workspace_connection(read_only=False) as (_, _, con):
        replay = get_library_replay(con, replay_id)
        events = parsed_events(con, replay_id)
    if replay is None:
        raise HTTPException(status_code=404, detail="Replay not found in the local review shelf.")
    try:
        parsed_payload = load_parsed_replay_frames(
            replay_id,
            hz=hz,
            max_frames=max_frames,
            start_frame=start_frame,
            serving_db=platform_paths()["serving_db"],
            local_file_path=replay.get("local_file_path"),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReplayParseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    payload = build_native_viewer_payload(replay, parsed_payload, events)
    encoded = store_native_viewer_payload_cache(
        replay_id,
        payload,
        hz=hz,
        max_frames=max_frames,
        start_frame=start_frame,
        parser_version=PARSER_VERSION,
    )
    return native_viewer_gzip_response(encoded, request)


@app.get("/library/replays")
def library_replays_alias(
    limit: int = Query(default=40, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = None,
    parsed_only: bool = False,
    review_ready: bool = False,
    sort: str = Query(default="recent"),
) -> dict[str, Any]:
    return api_replays(limit=limit, offset=offset, search=search, parsed_only=parsed_only, review_ready=review_ready, sort=sort)


@app.get("/library/replays/{replay_id}")
def library_replay_alias(replay_id: str) -> dict[str, Any]:
    return api_replay_detail(replay_id)


@app.get("/library/replays/{replay_id}/viewer")
def library_viewer_alias(replay_id: str) -> dict[str, Any]:
    return api_replay_viewer(replay_id)


@app.get("/library/replays/{replay_id}/file")
def library_file_alias(replay_id: str) -> FileResponse:
    return api_replay_file(replay_id)


@app.get("/library/replays/{replay_id}/native-viewer")
def library_native_viewer_alias(
    request: Request,
    replay_id: str,
    hz: int = Query(default=60, ge=1, le=60),
    max_frames: int = Query(default=24000, ge=300, le=250000),
    start_frame: int = Query(default=0, ge=0, le=250000),
) -> Response:
    return api_replay_native_viewer(request=request, replay_id=replay_id, hz=hz, max_frames=max_frames, start_frame=start_frame)


if __name__ == "__main__":
    uvicorn.run("standalone_replay_review:app", host="127.0.0.1", port=8010, reload=False)
