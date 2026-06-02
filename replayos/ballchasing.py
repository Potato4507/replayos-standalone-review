from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import duckdb

from .carball_ingest import ReplayParseError, ensure_replay_analysis
from .config import get_settings


class BallchasingError(RuntimeError):
    """Raised when the Ballchasing API fails."""


@dataclass
class BallchasingSyncResult:
    run_id: str
    seen: int
    inserted: int
    updated: int
    downloaded: int
    parsed: int
    parse_failed: int
    groups_synced: int
    players_upserted: int
    filters: dict[str, Any]


def ensure_ballchasing_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_sync_runs (
            run_id VARCHAR PRIMARY KEY,
            source VARCHAR,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            filters_json VARCHAR,
            result_json VARCHAR,
            status VARCHAR,
            error VARCHAR
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_remote_sync_runs_started_at ON remote_sync_runs (started_at)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_groups (
            group_id VARCHAR PRIMARY KEY,
            name VARCHAR,
            link VARCHAR,
            created_at TIMESTAMP,
            status VARCHAR,
            parent_id VARCHAR,
            player_identification VARCHAR,
            team_identification VARCHAR,
            creator_name VARCHAR,
            direct_replays BIGINT,
            indirect_replays BIGINT,
            players_json VARCHAR,
            raw_json VARCHAR,
            synced_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_replays (
            replay_id VARCHAR PRIMARY KEY,
            source VARCHAR,
            title VARCHAR,
            link VARCHAR,
            status VARCHAR,
            created_at TIMESTAMP,
            match_date TIMESTAMP,
            map_code VARCHAR,
            match_type VARCHAR,
            playlist_id VARCHAR,
            team_size BIGINT,
            duration DOUBLE,
            overtime BOOLEAN,
            overtime_seconds DOUBLE,
            season BIGINT,
            season_type VARCHAR,
            visibility VARCHAR,
            uploader_name VARCHAR,
            uploader_steam_id VARCHAR,
            blue_team_name VARCHAR,
            blue_goals BIGINT,
            orange_team_name VARCHAR,
            orange_goals BIGINT,
            group_ids_json VARCHAR,
            group_names_json VARCHAR,
            local_file_path VARCHAR,
            local_file_size BIGINT,
            downloaded_at TIMESTAMP,
            raw_json VARCHAR,
            synced_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_players (
            replay_id VARCHAR,
            side VARCHAR,
            platform VARCHAR,
            platform_player_id VARCHAR,
            player_name VARCHAR,
            car_name VARCHAR,
            score BIGINT,
            goals BIGINT,
            assists BIGINT,
            saves BIGINT,
            shots BIGINT,
            demos_inflicted BIGINT,
            demos_taken BIGINT,
            boost_bpm DOUBLE,
            avg_speed DOUBLE,
            percent_behind_ball DOUBLE,
            replay_title VARCHAR,
            match_date TIMESTAMP,
            PRIMARY KEY (replay_id, side, platform, platform_player_id)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_replay_groups (
            replay_id VARCHAR,
            group_id VARCHAR,
            group_name VARCHAR,
            PRIMARY KEY (replay_id, group_id)
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_remote_replays_match_date ON remote_replays (match_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_remote_replay_groups_group_id ON remote_replay_groups (group_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_remote_players_replay_side ON remote_players (replay_id, side)")


def prune_remote_sync_runs(con: duckdb.DuckDBPyConnection) -> None:
    keep_runs = max(10, int(get_settings().sync_run_retention))
    count = con.execute("SELECT COUNT(*) FROM remote_sync_runs").fetchone()[0]
    if int(count or 0) <= keep_runs:
        return
    overflow = int(count) - keep_runs
    con.execute(
        """
        DELETE FROM remote_sync_runs
        WHERE run_id IN (
            SELECT run_id
            FROM remote_sync_runs
            ORDER BY started_at ASC NULLS FIRST
            LIMIT ?
        )
        """,
        [overflow],
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("ballchasing_sync_%Y%m%dT%H%M%S%fZ")


def normalize_ballchasing_group_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme and parsed.netloc:
        pieces = [piece for piece in parsed.path.split("/") if piece]
        if "group" in pieces:
            index = pieces.index("group")
            if index + 1 < len(pieces):
                return pieces[index + 1]
        if "groups" in pieces:
            index = pieces.index("groups")
            if index + 1 < len(pieces):
                return pieces[index + 1]
    if "?" in normalized:
        return None
    return normalized


def normalize_ballchasing_creator_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme and parsed.netloc:
        creator_values = parse_qs(parsed.query).get("creator") or []
        if creator_values:
            return creator_values[0].strip() or None
        if parsed.path.strip("/").isdigit():
            return parsed.path.strip("/")
        return None
    if normalized.isdigit():
        return normalized
    return None


def resolve_ballchasing_source(value: str | None) -> tuple[str, str] | None:
    creator_id = normalize_ballchasing_creator_id(value)
    if creator_id:
        return ("creator", creator_id)
    group_id = normalize_ballchasing_group_id(value)
    if group_id:
        return ("group", group_id)
    return None


def configured_ballchasing_sources() -> dict[str, tuple[str, ...]]:
    settings = get_settings()
    groups = tuple(
        group_id
        for group_id in (normalize_ballchasing_group_id(value) for value in settings.ballchasing_default_groups)
        if group_id
    )
    creators = tuple(
        creator_id
        for creator_id in (normalize_ballchasing_creator_id(value) for value in settings.ballchasing_default_creators)
        if creator_id
    )
    return {"groups": groups, "creators": creators}


def expand_ballchasing_group_tree(
    client: "BallchasingClient",
    root_group_ids: list[str] | tuple[str, ...],
    *,
    max_children_per_group: int = 200,
) -> dict[str, Any]:
    roots = []
    for value in root_group_ids:
        group_id = normalize_ballchasing_group_id(value)
        if group_id and group_id not in roots:
            roots.append(group_id)

    queue = list(roots)
    visited: set[str] = set()
    ordered_groups: list[str] = []
    links: list[dict[str, str | None]] = []
    errors: list[dict[str, str]] = []

    while queue:
        group_id = queue.pop(0)
        if group_id in visited:
            continue
        visited.add(group_id)
        ordered_groups.append(group_id)
        try:
            children = client.iterate_groups(
                params={
                    "group": group_id,
                    "count": min(200, max(1, int(max_children_per_group))),
                    "sort-by": "created",
                    "sort-dir": "desc",
                },
                max_groups=min(200, max(1, int(max_children_per_group))),
            )
        except BallchasingError as exc:
            errors.append({"group_id": group_id, "error": str(exc)})
            continue

        for child in children:
            child_id = normalize_ballchasing_group_id(child.get("id") or child.get("link"))
            if not child_id:
                continue
            links.append(
                {
                    "parent_group_id": group_id,
                    "group_id": child_id,
                    "group_name": child.get("name"),
                }
            )
            if child_id not in visited and child_id not in queue:
                queue.append(child_id)

    return {
        "root_group_ids": roots,
        "group_ids": ordered_groups,
        "links": links,
        "errors": errors,
    }


class BallchasingClient:
    def __init__(self, *, api_base: str | None = None, token: str | None = None) -> None:
        settings = get_settings()
        self.api_base = (api_base or settings.ballchasing_api_base).rstrip("/")
        self.token = token or settings.ballchasing_api_token
        if not self.token:
            raise BallchasingError("BALLCHASING_API_TOKEN is not configured.")

    def ping(self) -> dict[str, Any]:
        return self._json_request("GET", "/")

    def list_replays(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._json_request("GET", "/replays", params=params)

    def list_groups(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._json_request("GET", "/groups", params=params)

    def iterate_replays(self, params: dict[str, Any] | None = None, *, max_replays: int = 25) -> list[dict[str, Any]]:
        payload = self.list_replays(params=params or {})
        items = list(payload.get("list") or [])
        next_url = payload.get("next")
        while next_url and len(items) < max_replays:
            page = self._json_request("GET", next_url)
            items.extend(page.get("list") or [])
            next_url = page.get("next")
        return items[:max_replays]

    def iterate_groups(self, params: dict[str, Any] | None = None, *, max_groups: int = 25) -> list[dict[str, Any]]:
        payload = self.list_groups(params=params or {})
        items = list(payload.get("list") or [])
        next_url = payload.get("next")
        while next_url and len(items) < max_groups:
            page = self._json_request("GET", next_url)
            items.extend(page.get("list") or [])
            next_url = page.get("next")
        return items[:max_groups]

    def get_replay(self, replay_id: str) -> dict[str, Any]:
        return self._json_request("GET", f"/replays/{replay_id}")

    def get_group(self, group_id: str) -> dict[str, Any]:
        return self._json_request("GET", f"/groups/{group_id}")

    def download_replay(self, replay_id: str, destination: Path, *, overwrite: bool = False) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not overwrite:
            return destination
        self._binary_request("GET", f"/replays/{replay_id}/file", destination)
        return destination

    def _json_request(self, method: str, path_or_url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self._request(method, path_or_url, params=params, binary_path=None)
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BallchasingError(f"Invalid JSON returned from Ballchasing for {path_or_url}") from exc

    def _binary_request(self, method: str, path_or_url: str, destination: Path) -> None:
        data = self._request(method, path_or_url, params=None, binary_path=destination)
        if data is not None:
            destination.write_bytes(data)

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None,
        binary_path: Path | None,
        retries: int = 4,
    ) -> bytes | None:
        url = path_or_url if path_or_url.startswith("http") else f"{self.api_base}{path_or_url}"
        if params:
            filtered = {key: value for key, value in params.items() if value not in (None, "", [], ())}
            if filtered:
                url = f"{url}?{urlencode(filtered, doseq=True)}"

        request = Request(
            url,
            method=method,
            headers={
                "Authorization": self.token or "",
                "User-Agent": "ReplayOS/0.2 (+local sync client)",
                "Accept": "application/json, application/octet-stream",
            },
        )
        for attempt in range(retries):
            try:
                with urlopen(request, timeout=60) as response:
                    if binary_path is not None:
                        with binary_path.open("wb") as handle:
                            shutil.copyfileobj(response, handle)
                        return None
                    return response.read()
            except HTTPError as exc:
                if exc.code == 429 and attempt < retries - 1:
                    retry_after = exc.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 1.0 + attempt
                    except ValueError:
                        delay = 1.0 + attempt
                    time.sleep(delay)
                    continue
                body = exc.read().decode("utf-8", errors="replace")
                raise BallchasingError(f"{exc.code} from Ballchasing at {url}: {body}") from exc
        raise BallchasingError(f"Retries exhausted for {url}")


def sync_ballchasing_replays(
    serving_db: Path | None = None,
    *,
    filters: dict[str, Any] | None = None,
    count: int | None = None,
    download_files: bool = True,
    fetch_details: bool = True,
    force_download: bool = False,
    parse_downloads: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    serving_db = Path(serving_db or settings.serving_db)
    client = BallchasingClient()
    filters = dict(filters or {})
    replay_limit = int(count or filters.pop("count", settings.ballchasing_default_count))
    run_id = _run_id()
    started_at = datetime.now(timezone.utc)
    items = client.iterate_replays(filters, max_replays=replay_limit)

    con = duckdb.connect(str(serving_db))
    try:
        ensure_ballchasing_schema(con)
        con.execute(
            "INSERT OR REPLACE INTO remote_sync_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, "ballchasing", started_at, None, _json(filters), None, "running", None],
        )

        seen = 0
        inserted = 0
        updated = 0
        downloaded = 0
        parsed = 0
        parse_failed = 0
        groups_synced = 0
        players_upserted = 0
        group_cache: set[str] = set()
        parse_errors: list[dict[str, Any]] = []

        for item in items:
            seen += 1
            replay_id = item["id"]
            detail = client.get_replay(replay_id) if fetch_details else item
            existed = con.execute("SELECT 1 FROM remote_replays WHERE replay_id = ?", [replay_id]).fetchone() is not None
            if existed:
                updated += 1
            else:
                inserted += 1
            groups = detail.get("groups") or item.get("groups") or []
            group_ids = [group.get("id") for group in groups if group.get("id")]
            group_names = [group.get("name") for group in groups if group.get("name")]
            blue = detail.get("blue") or item.get("blue") or {}
            orange = detail.get("orange") or item.get("orange") or {}
            local_path = settings.replay_download_dir / f"{replay_id}.replay"
            if download_files:
                before = local_path.exists()
                client.download_replay(replay_id, local_path, overwrite=force_download)
                if not before or force_download:
                    downloaded += 1
            con.execute(
                """
                INSERT OR REPLACE INTO remote_replays VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    replay_id,
                    "ballchasing",
                    detail.get("title") or item.get("title"),
                    detail.get("link") or item.get("link"),
                    detail.get("status") or item.get("status"),
                    _parse_dt(detail.get("created") or item.get("created")),
                    _parse_dt(detail.get("date") or item.get("date")),
                    detail.get("map_code") or item.get("map_code"),
                    detail.get("match_type") or item.get("match_type"),
                    detail.get("playlist_id") or item.get("playlist_id"),
                    detail.get("team_size") or item.get("team_size"),
                    detail.get("duration") or item.get("duration"),
                    detail.get("overtime") if detail.get("overtime") is not None else item.get("overtime"),
                    detail.get("overtime_seconds"),
                    detail.get("season") or item.get("season"),
                    detail.get("season_type") or item.get("season_type"),
                    detail.get("visibility") or item.get("visibility"),
                    (detail.get("uploader") or item.get("uploader") or {}).get("name"),
                    (detail.get("uploader") or item.get("uploader") or {}).get("steam_id"),
                    blue.get("name"),
                    blue.get("goals"),
                    orange.get("name"),
                    orange.get("goals"),
                    _json(group_ids),
                    _json(group_names),
                    str(local_path) if local_path.exists() else None,
                    local_path.stat().st_size if local_path.exists() else None,
                    datetime.now(timezone.utc) if local_path.exists() else None,
                    _json(detail),
                    datetime.now(timezone.utc),
                ],
            )
            for side_name, side_data in (("blue", blue), ("orange", orange)):
                for player in side_data.get("players") or []:
                    stats = player.get("stats") or {}
                    core = stats.get("core") or {}
                    demo = stats.get("demo") or {}
                    boost = stats.get("boost") or {}
                    movement = stats.get("movement") or {}
                    positioning = stats.get("positioning") or {}
                    identifier = player.get("id") or {}
                    con.execute(
                        """
                        INSERT OR REPLACE INTO remote_players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            replay_id,
                            side_name,
                            identifier.get("platform"),
                            identifier.get("id"),
                            player.get("name"),
                            player.get("car_name"),
                            core.get("score"),
                            core.get("goals"),
                            core.get("assists"),
                            core.get("saves"),
                            core.get("shots"),
                            demo.get("inflicted"),
                            demo.get("taken"),
                            boost.get("bpm"),
                            movement.get("avg_speed"),
                            positioning.get("percent_behind_ball"),
                            detail.get("title") or item.get("title"),
                            _parse_dt(detail.get("date") or item.get("date")),
                        ],
                    )
                    players_upserted += 1

            for group in groups:
                if not group.get("id"):
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO remote_replay_groups VALUES (?, ?, ?)",
                    [replay_id, group.get("id"), group.get("name")],
                )

            for group_id in group_ids:
                if not group_id or group_id in group_cache:
                    continue
                group_cache.add(group_id)
                group_detail = client.get_group(group_id)
                con.execute(
                    """
                    INSERT OR REPLACE INTO remote_groups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        group_detail.get("id"),
                        group_detail.get("name"),
                        group_detail.get("link"),
                        _parse_dt(group_detail.get("created")),
                        group_detail.get("status"),
                        group_detail.get("parent"),
                        group_detail.get("player_identification"),
                        group_detail.get("team_identification"),
                        (group_detail.get("creator") or {}).get("name"),
                        group_detail.get("direct_replays"),
                        group_detail.get("indirect_replays"),
                        _json(group_detail.get("players") or []),
                        _json(group_detail),
                        datetime.now(timezone.utc),
                    ],
                )
                groups_synced += 1

            if parse_downloads and local_path.exists():
                try:
                    result = ensure_replay_analysis(
                        replay_id,
                        local_file_path=local_path,
                        serving_db=serving_db,
                        force=force_download,
                    )
                    if result.get("status") == "completed":
                        parsed += 1
                except ReplayParseError as exc:
                    parse_failed += 1
                    parse_errors.append({"replay_id": replay_id, "error": str(exc)})

        result = BallchasingSyncResult(
            run_id=run_id,
            seen=seen,
            inserted=inserted,
            updated=updated,
            downloaded=downloaded,
            parsed=parsed,
            parse_failed=parse_failed,
            groups_synced=groups_synced,
            players_upserted=players_upserted,
            filters=filters,
        )
        completed_at = datetime.now(timezone.utc)
        con.execute(
            "INSERT OR REPLACE INTO remote_sync_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                "ballchasing",
                started_at,
                completed_at,
                _json(filters),
                _json(result.__dict__),
                "completed",
                None,
            ],
        )
        prune_remote_sync_runs(con)
        return {
            **result.__dict__,
            "download_dir": str(settings.replay_download_dir),
            "completed_at": completed_at.isoformat(),
            "parse_errors": parse_errors,
        }
    except Exception as exc:
        completed_at = datetime.now(timezone.utc)
        ensure_ballchasing_schema(con)
        con.execute(
            "INSERT OR REPLACE INTO remote_sync_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                "ballchasing",
                started_at,
                completed_at,
                _json(filters),
                None,
                "failed",
                str(exc),
            ],
        )
        prune_remote_sync_runs(con)
        raise
    finally:
        con.close()


def ensure_ballchasing_replay_download(
    replay_id: str,
    serving_db: Path | None = None,
    *,
    force_download: bool = False,
    parse_download: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    serving_db = Path(serving_db or settings.serving_db)
    client = BallchasingClient()
    detail = client.get_replay(replay_id)
    blue = detail.get("blue") or {}
    orange = detail.get("orange") or {}
    groups = detail.get("groups") or []
    group_ids = [group.get("id") for group in groups if group.get("id")]
    group_names = [group.get("name") for group in groups if group.get("name")]
    local_path = settings.replay_download_dir / f"{replay_id}.replay"
    existed_before = local_path.exists()
    client.download_replay(replay_id, local_path, overwrite=force_download)

    con = duckdb.connect(str(serving_db))
    try:
        ensure_ballchasing_schema(con)
        con.execute(
            """
            INSERT OR REPLACE INTO remote_replays VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                replay_id,
                "ballchasing",
                detail.get("title"),
                detail.get("link"),
                detail.get("status"),
                _parse_dt(detail.get("created")),
                _parse_dt(detail.get("date")),
                detail.get("map_code"),
                detail.get("match_type"),
                detail.get("playlist_id"),
                detail.get("team_size"),
                detail.get("duration"),
                detail.get("overtime"),
                detail.get("overtime_seconds"),
                detail.get("season"),
                detail.get("season_type"),
                detail.get("visibility"),
                (detail.get("uploader") or {}).get("name"),
                (detail.get("uploader") or {}).get("steam_id"),
                blue.get("name"),
                blue.get("goals"),
                orange.get("name"),
                orange.get("goals"),
                _json(group_ids),
                _json(group_names),
                str(local_path) if local_path.exists() else None,
                local_path.stat().st_size if local_path.exists() else None,
                datetime.now(timezone.utc) if local_path.exists() else None,
                _json(detail),
                datetime.now(timezone.utc),
            ],
        )
        for side_name, side_data in (("blue", blue), ("orange", orange)):
            for player in side_data.get("players") or []:
                stats = player.get("stats") or {}
                core = stats.get("core") or {}
                demo = stats.get("demo") or {}
                boost = stats.get("boost") or {}
                movement = stats.get("movement") or {}
                positioning = stats.get("positioning") or {}
                identifier = player.get("id") or {}
                con.execute(
                    """
                    INSERT OR REPLACE INTO remote_players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        replay_id,
                        side_name,
                        identifier.get("platform"),
                        identifier.get("id"),
                        player.get("name"),
                        player.get("car_name"),
                        core.get("score"),
                        core.get("goals"),
                        core.get("assists"),
                        core.get("saves"),
                        core.get("shots"),
                        demo.get("inflicted"),
                        demo.get("taken"),
                        boost.get("bpm"),
                        movement.get("avg_speed"),
                        positioning.get("percent_behind_ball"),
                        detail.get("title"),
                        _parse_dt(detail.get("date")),
                    ],
                )
        for group in groups:
            if not group.get("id"):
                continue
            con.execute(
                "INSERT OR REPLACE INTO remote_replay_groups VALUES (?, ?, ?)",
                [replay_id, group.get("id"), group.get("name")],
            )
    finally:
        con.close()

    parse_result = None
    parse_error = None
    if parse_download and local_path.exists():
        try:
            parse_result = ensure_replay_analysis(
                replay_id,
                local_file_path=local_path,
                serving_db=serving_db,
                force=force_download,
            )
        except ReplayParseError as exc:
            parse_error = str(exc)

    return {
        "replay_id": replay_id,
        "downloaded": (not existed_before) or force_download,
        "parsed": bool(parse_result and parse_result.get("status") == "completed"),
        "parse_result": parse_result,
        "parse_error": parse_error,
        "local_file_path": str(local_path) if local_path.exists() else None,
    }


def sync_ballchasing_source_set(
    serving_db: Path | None = None,
    *,
    group_ids: list[str] | tuple[str, ...] | None = None,
    creator_ids: list[str] | tuple[str, ...] | None = None,
    creator_group_limit: int | None = None,
    base_filters: dict[str, Any] | None = None,
    count: int | None = None,
    download_files: bool = True,
    fetch_details: bool = True,
    force_download: bool = False,
    parse_downloads: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    client = BallchasingClient()
    root_groups = []
    for value in group_ids or ():
        group_id = normalize_ballchasing_group_id(value)
        if group_id and group_id not in root_groups:
            root_groups.append(group_id)
    normalized_creators = []
    for value in creator_ids or ():
        creator_id = normalize_ballchasing_creator_id(value)
        if creator_id and creator_id not in normalized_creators:
            normalized_creators.append(creator_id)

    per_creator_limit = max(1, int(creator_group_limit or settings.ballchasing_default_creator_group_limit))
    creator_groups: list[dict[str, Any]] = []
    creator_errors: list[dict[str, str]] = []
    for creator_id in normalized_creators:
        try:
            rows = client.iterate_groups(
                params={
                    "creator": creator_id,
                    "count": min(200, per_creator_limit),
                    "sort-by": "created",
                    "sort-dir": "desc",
                },
                max_groups=per_creator_limit,
            )
        except BallchasingError as exc:
            creator_errors.append({"creator_id": creator_id, "error": str(exc)})
            continue
        for row in rows:
            group_id = normalize_ballchasing_group_id(row.get("id") or row.get("link"))
            if not group_id:
                continue
            creator_groups.append(
                {
                    "creator_id": creator_id,
                    "group_id": group_id,
                    "group_name": row.get("name"),
                    "created": row.get("created"),
                }
            )
            if group_id not in root_groups:
                root_groups.append(group_id)

    if not root_groups:
        raise BallchasingError("No Ballchasing group ids were resolved from the configured defaults or requested sources.")

    tree = expand_ballchasing_group_tree(client, root_groups)
    parent_group_ids = {str(link["parent_group_id"]) for link in tree["links"] if link.get("parent_group_id")}
    leaf_groups = [group_id for group_id in tree["group_ids"] if group_id not in parent_group_ids]
    expanded_groups = leaf_groups or list(tree["group_ids"])
    if not expanded_groups:
        raise BallchasingError("Ballchasing source discovery resolved zero concrete groups to sync.")

    totals = {
        "seen": 0,
        "inserted": 0,
        "updated": 0,
        "downloaded": 0,
        "parsed": 0,
        "parse_failed": 0,
        "groups_synced": 0,
        "players_upserted": 0,
    }
    source_runs: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = list(tree["errors"])
    for group_id in expanded_groups:
        filters = dict(base_filters or {})
        filters["group"] = group_id
        result = sync_ballchasing_replays(
            serving_db,
            filters=filters,
            count=count,
            download_files=download_files,
            fetch_details=fetch_details,
            force_download=force_download,
            parse_downloads=parse_downloads,
        )
        for key in totals:
            totals[key] += int(result.get(key) or 0)
        parse_errors.extend(result.get("parse_errors") or [])
        source_runs.append(
            {
                "group_id": group_id,
                "seen": result.get("seen", 0),
                "downloaded": result.get("downloaded", 0),
                "parsed": result.get("parsed", 0),
                "parse_failed": result.get("parse_failed", 0),
            }
        )

    return {
        **totals,
        "group_ids": root_groups,
        "leaf_group_ids": leaf_groups,
        "expanded_group_ids": expanded_groups,
        "creator_ids": normalized_creators,
        "creator_group_limit": per_creator_limit,
        "creator_groups": creator_groups,
        "creator_errors": creator_errors,
        "group_tree_links": tree["links"],
        "sources": source_runs,
        "parse_errors": [*creator_errors, *parse_errors],
        "download_dir": str(get_settings().replay_download_dir),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
