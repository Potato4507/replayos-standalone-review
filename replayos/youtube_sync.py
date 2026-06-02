from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import duckdb

try:
    import yt_dlp
except ImportError:  # pragma: no cover - optional at runtime, installed in dev/test setup
    yt_dlp = None

from .config import get_settings


CHAPTER_PREFIX_PATTERN = re.compile(
    r"^\s*(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\s*(?:[-|:>\]\[)\s]*)?(?P<label>.+?)\s*$"
)
CHAPTER_SUFFIX_PATTERN = re.compile(
    r"^\s*(?P<label>.+?)\s*(?:[-|:]\s*)(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\s*$"
)
GAME_NUMBER_PATTERN = re.compile(r"\b(?:game|g|match)\s*#?\s*(\d{1,2})\b", re.IGNORECASE)
GAME_WORDS = (
    "game ",
    " match",
    "vs",
    "grand final",
    "lower final",
    "upper final",
    "quarterfinal",
    "quarter-final",
    "semifinal",
    "semi-final",
    "playoff",
)
NON_GAME_WORDS = ("desk", "break", "intro", "outro", "countdown", "waiting room", "interview")
BAD_VIDEO_TOKENS: tuple[tuple[str, float], ...] = (
    ("highlight", -4.4),
    ("highlights", -4.4),
    ("official highlights", -4.8),
    ("recap", -2.6),
    ("reaction", -3.4),
    ("reacts", -3.4),
    ("watch party", -3.6),
    ("comms", -3.8),
    ("leaked", -3.2),
    ("pov", -6.0),
    ("streamers react", -4.2),
    ("clip", -1.8),
    ("shorts", -3.4),
)
GOOD_VIDEO_TOKENS: tuple[tuple[str, float], ...] = (
    ("full match", 3.2),
    ("full series", 2.3),
    ("vod", 1.2),
    ("day ", 0.6),
    ("game ", 0.8),
)
MIN_VIDEO_MATCH_SCORE = 4.0
MAX_SERIES_VOD_ESTIMATE_RATIO = 4.5
MIN_ESTIMATED_SEGMENT_SCORE = 3.2


class YouTubeSyncError(RuntimeError):
    """Raised when YouTube sync fails."""


@dataclass
class YouTubeSyncResult:
    run_id: str
    replay_count: int
    searched: int
    linked: int
    segmented: int
    query_scope: str


def ensure_youtube_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS youtube_sync_runs (
            run_id VARCHAR PRIMARY KEY,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            filters_json VARCHAR,
            result_json VARCHAR,
            status VARCHAR,
            error VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS replay_videos (
            replay_id VARCHAR,
            video_id VARCHAR,
            title VARCHAR,
            channel_title VARCHAR,
            published_at TIMESTAMP,
            description VARCHAR,
            thumbnail_url VARCHAR,
            duration_iso8601 VARCHAR,
            view_count BIGINT,
            embed_url VARCHAR,
            watch_url VARCHAR,
            query_text VARCHAR,
            match_score DOUBLE,
            reasons_json VARCHAR,
            synced_at TIMESTAMP,
            source_video_id VARCHAR,
            duration_seconds DOUBLE,
            video_kind VARCHAR,
            segment_label VARCHAR,
            segment_start_seconds BIGINT,
            segment_end_seconds BIGINT,
            segment_confidence DOUBLE,
            PRIMARY KEY (replay_id, video_id)
        )
        """
    )
    _ensure_columns(
        con,
        "replay_videos",
        {
            "source_video_id": "VARCHAR",
            "duration_seconds": "DOUBLE",
            "video_kind": "VARCHAR",
            "segment_label": "VARCHAR",
            "segment_start_seconds": "BIGINT",
            "segment_end_seconds": "BIGINT",
            "segment_confidence": "DOUBLE",
        },
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_replay_videos_replay_score ON replay_videos (replay_id, match_score)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_youtube_sync_runs_started_at ON youtube_sync_runs (started_at)")


def _ensure_columns(con: duckdb.DuckDBPyConnection, table_name: str, column_defs: dict[str, str]) -> None:
    existing = _table_columns(con, table_name)
    for column_name, column_type in column_defs.items():
        if column_name not in existing:
            con.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _table_columns(con: duckdb.DuckDBPyConnection, table_name: str) -> set[str]:
    rows = con.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'main' AND table_name = ?
        """,
        [table_name],
    ).fetchall()
    return {row[0] for row in rows}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("youtube_sync_%Y%m%dT%H%M%S%fZ")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


class YouTubeClient:
    def __init__(self, *, api_key: str | None = None, api_base: str | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.youtube_api_key
        self.api_base = (api_base or settings.youtube_api_base).rstrip("/")
        if self.api_key:
            self.provider = "youtube_data_api"
        elif yt_dlp is not None:
            self.provider = "yt_dlp_public"
        else:
            raise YouTubeSyncError("ReplayOS needs either YOUTUBE_API_KEY or the yt-dlp package for public video search.")

    @staticmethod
    def provider_status(*, api_key: str | None = None) -> dict[str, Any]:
        settings = get_settings()
        active_key = api_key or settings.youtube_api_key
        if active_key:
            return {"provider": "youtube_data_api", "sync_enabled": True, "requires_key": False}
        if yt_dlp is not None:
            return {"provider": "yt_dlp_public", "sync_enabled": True, "requires_key": False}
        return {"provider": None, "sync_enabled": False, "requires_key": True}

    def search_videos(self, query: str, *, max_results: int = 5, published_after: datetime | None = None) -> list[dict[str, Any]]:
        if self.provider == "yt_dlp_public":
            return self._search_videos_public(query, max_results=max_results, published_after=published_after)
        params: dict[str, Any] = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max_results,
            "key": self.api_key,
            "videoEmbeddable": "true",
        }
        if published_after:
            params["publishedAfter"] = published_after.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = self._request("/search", params)
        return payload.get("items") or []

    def videos(self, video_ids: list[str]) -> list[dict[str, Any]]:
        if not video_ids:
            return []
        if self.provider == "yt_dlp_public":
            return [video for video in (self._video_public(video_id) for video_id in video_ids) if video]
        params = {
            "part": "snippet,contentDetails,statistics",
            "id": ",".join(video_ids),
            "key": self.api_key,
        }
        payload = self._request("/videos", params)
        return payload.get("items") or []

    def _search_videos_public(
        self,
        query: str,
        *,
        max_results: int,
        published_after: datetime | None,
    ) -> list[dict[str, Any]]:
        entries = self._extract_with_ytdlp(
            f"ytsearch{max_results}:{query}",
            {
                "extract_flat": "in_playlist",
                "playlist_items": f"1:{max_results}",
            },
        ).get("entries") or []
        items: list[dict[str, Any]] = []
        for entry in entries:
            published_at = _yt_dlp_published_at(entry)
            if published_after and published_at:
                boundary = published_after.astimezone(timezone.utc) if published_after.tzinfo else published_after.replace(tzinfo=timezone.utc)
                if published_at < boundary:
                    continue
            video_id = entry.get("id")
            if not video_id:
                continue
            items.append({"id": {"videoId": video_id}})
        return items

    def _video_public(self, video_id: str) -> dict[str, Any] | None:
        info = self._extract_with_ytdlp(
            f"https://www.youtube.com/watch?v={video_id}",
            {
                "extract_flat": False,
                "noplaylist": True,
            },
        )
        if not info or not info.get("id"):
            return None
        return _normalize_ytdlp_video(info)

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.api_base}{path}?{urlencode(params, doseq=True)}"
        request = Request(url, headers={"User-Agent": "ReplayOS/0.3 (+youtube sync)"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise YouTubeSyncError(f"YouTube request failed for {path}: {exc}") from exc

    def _extract_with_ytdlp(self, target: str, extra_opts: dict[str, Any] | None = None) -> dict[str, Any]:
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "ignoreerrors": True,
            "socket_timeout": 30,
        }
        if extra_opts:
            options.update(extra_opts)
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(target, download=False) or {}
        except Exception as exc:
            raise YouTubeSyncError(f"Public YouTube fetch failed for {target}: {exc}") from exc


def sync_youtube_videos(
    serving_db: Path | None = None,
    *,
    replay_id: str | None = None,
    limit: int | None = None,
    max_results_per_replay: int = 4,
) -> dict[str, Any]:
    settings = get_settings()
    serving_db = Path(serving_db or settings.serving_db)
    client = YouTubeClient()
    run_id = _run_id()
    started_at = datetime.now(timezone.utc)

    con = duckdb.connect(str(serving_db))
    try:
        ensure_youtube_schema(con)
        filters = {"replay_id": replay_id, "limit": limit}
        con.execute(
            "INSERT OR REPLACE INTO youtube_sync_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            [run_id, started_at, None, _json(filters), None, "running", None],
        )

        candidates = _replay_candidates(con, replay_id=replay_id, limit=limit or settings.youtube_default_count)
        persist_candidates = [candidate for candidate in candidates if candidate.get("requested", not replay_id)]
        replay_results: dict[str, list[dict[str, Any]]] = {candidate["replay_id"]: [] for candidate in candidates}
        searched = 0
        segmented = 0

        for candidate in candidates:
            query = _build_query(candidate)
            search_items = client.search_videos(query, max_results=6, published_after=_published_after(candidate))
            searched += 1
            ids = [item.get("id", {}).get("videoId") for item in search_items if item.get("id", {}).get("videoId")]
            videos = client.videos(ids)
            scored = sorted(
                (_score_video(candidate, video) for video in videos),
                key=lambda item: item["match_score"],
                reverse=True,
            )
            replay_results[candidate["replay_id"]].extend([item for item in scored if item["match_score"] > 0])

        for bundle in _series_bundles(candidates):
            query = _build_series_vod_query(bundle)
            if not query:
                continue
            search_items = client.search_videos(query, max_results=6, published_after=_bundle_published_after(bundle))
            searched += 1
            ids = [item.get("id", {}).get("videoId") for item in search_items if item.get("id", {}).get("videoId")]
            videos = client.videos(ids)
            scored_vods = sorted(
                (_score_series_vod(bundle, video) for video in videos),
                key=lambda item: item["match_score"],
                reverse=True,
            )[:3]
            for vod in scored_vods:
                if vod["match_score"] <= 0:
                    continue
                assignments = _assign_bundle_video_items(bundle, vod)
                for assigned_replay_id, items in assignments.items():
                    replay_results.setdefault(assigned_replay_id, []).extend(items)
                    segmented += len(items)

        linked = 0
        for candidate in persist_candidates:
            replay_id_value = candidate["replay_id"]
            items = [
                item
                for item in _dedupe_video_items(replay_results.get(replay_id_value) or [])
                if float(item.get("match_score") or 0.0) >= MIN_VIDEO_MATCH_SCORE
            ][:max_results_per_replay]
            con.execute("DELETE FROM replay_videos WHERE replay_id = ?", [replay_id_value])
            for item in items:
                con.execute(
                    """
                    INSERT INTO replay_videos (
                        replay_id, video_id, title, channel_title, published_at, description, thumbnail_url,
                        duration_iso8601, view_count, embed_url, watch_url, query_text, match_score,
                        reasons_json, synced_at, source_video_id, duration_seconds, video_kind, segment_label,
                        segment_start_seconds, segment_end_seconds, segment_confidence
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        replay_id_value,
                        item["video_id"],
                        item["title"],
                        item["channel_title"],
                        _parse_dt(item["published_at"]),
                        item["description"],
                        item["thumbnail_url"],
                        item["duration_iso8601"],
                        item["view_count"],
                        item["embed_url"],
                        item["watch_url"],
                        item["query_text"],
                        item["match_score"],
                        _json(item["reasons"]),
                        datetime.now(timezone.utc),
                        item["source_video_id"],
                        item["duration_seconds"],
                        item["video_kind"],
                        item.get("segment_label"),
                        item.get("segment_start_seconds"),
                        item.get("segment_end_seconds"),
                        item.get("segment_confidence"),
                    ],
                )
                linked += 1

        result = YouTubeSyncResult(
            run_id=run_id,
            replay_count=len(persist_candidates),
            searched=searched,
            linked=linked,
            segmented=segmented,
            query_scope="single-series-context" if replay_id and len(candidates) > len(persist_candidates) else "single" if replay_id else "batch",
        )
        completed_at = datetime.now(timezone.utc)
        con.execute(
            "INSERT OR REPLACE INTO youtube_sync_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            [run_id, started_at, completed_at, _json(filters), _json(result.__dict__), "completed", None],
        )
        return {**result.__dict__, "completed_at": completed_at.isoformat()}
    except Exception as exc:
        completed_at = datetime.now(timezone.utc)
        ensure_youtube_schema(con)
        con.execute(
            "INSERT OR REPLACE INTO youtube_sync_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            [run_id, started_at, completed_at, _json({"replay_id": replay_id, "limit": limit}), None, "failed", str(exc)],
        )
        raise
    finally:
        con.close()


def youtube_status(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    if not _table_exists(con, "youtube_sync_runs"):
        return {"configured_tables": False, "videos": 0, "last_run": None}
    rows = con.execute(
        """
        SELECT run_id, started_at, completed_at, status, error, result_json
        FROM youtube_sync_runs
        ORDER BY started_at DESC
        LIMIT 1
        """
    ).fetchall()
    last_run = None
    if rows:
        row = rows[0]
        last_run = {
            "run_id": row[0],
            "started_at": row[1].isoformat() if row[1] else None,
            "completed_at": row[2].isoformat() if row[2] else None,
            "status": row[3],
            "error": row[4],
            "result": json.loads(row[5]) if row[5] else None,
        }
    count = con.execute("SELECT COUNT(*) FROM replay_videos").fetchone()[0] if _table_exists(con, "replay_videos") else 0
    return {"configured_tables": True, "videos": count, "last_run": last_run}


def replay_videos(con: duckdb.DuckDBPyConnection, replay_id: str) -> list[dict[str, Any]]:
    if not _table_exists(con, "replay_videos"):
        return []
    columns = _table_columns(con, "replay_videos")
    rows = con.execute(
        f"""
        SELECT replay_id, video_id, title, channel_title, published_at, description, thumbnail_url,
               duration_iso8601, view_count, embed_url, watch_url, query_text, match_score, reasons_json, synced_at,
               {_optional_column(columns, 'source_video_id')},
               {_optional_column(columns, 'duration_seconds')},
               {_optional_column(columns, 'video_kind')},
               {_optional_column(columns, 'segment_label')},
               {_optional_column(columns, 'segment_start_seconds')},
               {_optional_column(columns, 'segment_end_seconds')},
               {_optional_column(columns, 'segment_confidence')}
        FROM replay_videos
        WHERE replay_id = ?
        ORDER BY
            COALESCE({_optional_column(columns, 'segment_confidence')}, 0.0) DESC,
            CASE
                WHEN {_optional_column(columns, 'video_kind')} = 'vod_segment' THEN 2
                WHEN {_optional_column(columns, 'video_kind')} = 'vod_estimate' THEN 1
                ELSE 0
            END DESC,
            match_score DESC,
            published_at DESC NULLS LAST
        """,
        [replay_id],
    ).fetchall()
    output = []
    for row in rows:
        output.append(
            {
                "replay_id": row[0],
                "video_id": row[1],
                "title": row[2],
                "channel_title": row[3],
                "published_at": row[4].isoformat() if row[4] else None,
                "description": row[5],
                "thumbnail_url": row[6],
                "duration_iso8601": row[7],
                "view_count": row[8],
                "embed_url": row[9],
                "watch_url": row[10],
                "query_text": row[11],
                "match_score": row[12],
                "reasons": json.loads(row[13]) if row[13] else [],
                "synced_at": row[14].isoformat() if row[14] else None,
                "source_video_id": row[15] or row[1],
                "duration_seconds": row[16],
                "video_kind": row[17] or "full_video",
                "segment_label": row[18],
                "segment_start_seconds": row[19],
                "segment_end_seconds": row[20],
                "segment_confidence": row[21],
            }
        )
    return output


def _optional_column(columns: set[str], column_name: str) -> str:
    return column_name if column_name in columns else "NULL"


def _candidate_rows(con: duckdb.DuckDBPyConnection, *, replay_id: str | None, limit: int | None) -> list[tuple[Any, ...]]:
    if not _table_exists(con, "remote_replays"):
        raise YouTubeSyncError("remote_replays does not exist yet. Run a Ballchasing sync before syncing YouTube videos.")
    remote_columns = _table_columns(con, "remote_replays")
    group_ids_expr = "rr.group_ids_json" if "group_ids_json" in remote_columns else "NULL"
    group_names_expr = "rr.group_names_json" if "group_names_json" in remote_columns else "NULL"
    filters = "WHERE rr.replay_id = ?" if replay_id else ""
    params: list[Any] = [replay_id] if replay_id else []
    if not replay_id and limit is not None:
        params.append(limit * 3)
    sql = f"""
        SELECT
            rr.replay_id,
            rr.title,
            rr.match_date,
            rr.created_at,
            rr.blue_team_name,
            rr.orange_team_name,
            rr.duration,
            rg.group_id,
            COALESCE(rg.group_name, rg2.name) AS series_name,
            {group_ids_expr} AS group_ids_json,
            {group_names_expr} AS group_names_json
        FROM remote_replays rr
        LEFT JOIN remote_replay_groups rg USING (replay_id)
        LEFT JOIN remote_groups rg2 ON rg.group_id = rg2.group_id
        {filters}
        ORDER BY COALESCE(rr.match_date, rr.created_at) DESC NULLS LAST, rr.replay_id, rg.group_name
    """
    if not replay_id and limit is not None:
        sql += "\nLIMIT ?"
    return con.execute(sql, params).fetchall()


def _candidate_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "replay_id": row[0],
        "title": row[1],
        "match_date": row[2],
        "created_at": row[3],
        "blue_team_name": row[4],
        "orange_team_name": row[5],
        "duration": float(row[6] or 0.0) if row[6] is not None else None,
        "group_id": row[7],
        "series_name": row[8],
        "group_ids_json": row[9],
        "group_names_json": row[10],
    }


def _deduped_candidates(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        replay_entry = _candidate_from_row(row)
        _apply_series_group_fallback(replay_entry)
        existing = deduped.get(replay_entry["replay_id"])
        if existing is None or (not existing.get("group_id") and replay_entry.get("group_id")) or (
            not existing.get("series_name") and replay_entry.get("series_name")
        ):
            deduped[replay_entry["replay_id"]] = replay_entry
    candidates = list(deduped.values())
    candidates.sort(
        key=lambda item: (
            item.get("match_date") or item.get("created_at") or datetime.min,
            item["replay_id"],
        )
    )
    _annotate_series_order(candidates)
    return candidates


def _series_context_candidates(
    con: duckdb.DuckDBPyConnection,
    requested_candidate: dict[str, Any],
    *,
    sibling_limit: int = 8,
) -> list[dict[str, Any]]:
    rows = _candidate_rows(con, replay_id=None, limit=max(sibling_limit * 6, 48))
    candidates = _deduped_candidates(rows)
    series_ids = {value for value in [requested_candidate.get("group_id"), *_safe_json_list(requested_candidate.get("group_ids_json"))] if value}
    fallback_key = _series_fallback_key(requested_candidate)
    siblings: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("replay_id") == requested_candidate.get("replay_id"):
            continue
        candidate_series_ids = {value for value in [candidate.get("group_id"), *_safe_json_list(candidate.get("group_ids_json"))] if value}
        same_series = bool(series_ids and candidate_series_ids.intersection(series_ids))
        if not same_series and fallback_key:
            same_series = _series_fallback_key(candidate) == fallback_key
        if same_series:
            siblings.append(candidate)
    siblings.sort(key=lambda item: (item.get("series_replay_index") or 9999, item.get("match_date") or item.get("created_at") or datetime.min, item["replay_id"]))
    return siblings[:sibling_limit]


def _replay_candidates(con: duckdb.DuckDBPyConnection, *, replay_id: str | None, limit: int) -> list[dict[str, Any]]:
    rows = _candidate_rows(con, replay_id=replay_id, limit=limit if not replay_id else None)
    candidates = _deduped_candidates(rows)
    if replay_id:
        if not candidates:
            return []
        requested = dict(candidates[0])
        requested["requested"] = True
        siblings = _series_context_candidates(con, requested)
        merged = [requested]
        for sibling in siblings:
            sibling = dict(sibling)
            sibling["requested"] = False
            merged.append(sibling)
        _annotate_series_order(merged)
        return merged
    for candidate in candidates:
        candidate["requested"] = True
    return candidates[-limit:]


def _apply_series_group_fallback(candidate: dict[str, Any]) -> None:
    group_ids = _safe_json_list(candidate.get("group_ids_json"))
    group_names = _safe_json_list(candidate.get("group_names_json"))
    current_score = _series_group_specificity(str(candidate.get("series_name") or ""), candidate)
    if not group_ids and not group_names and current_score > -100.0:
        return
    scored = []
    max_len = max(len(group_ids), len(group_names))
    for index in range(max_len):
        group_id = group_ids[index] if index < len(group_ids) else None
        group_name = group_names[index] if index < len(group_names) else None
        if not group_id and not group_name:
            continue
        scored.append((_series_group_specificity(group_name or "", candidate), group_id, group_name))
    if not scored:
        return
    best_score, group_id, group_name = max(scored, key=lambda item: item[0])
    if candidate.get("group_id") and candidate.get("series_name") and current_score >= best_score:
        return
    candidate["group_id"] = group_id or candidate.get("group_id")
    candidate["series_name"] = group_name or candidate.get("series_name")


def _safe_json_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return []


def _series_group_specificity(group_name: str, candidate: dict[str, Any]) -> float:
    label = (group_name or "").strip().lower()
    if not label:
        return -100.0
    score = 0.0
    if " vs " in label or " vs." in label:
        score += 6.5
    if any(token in label for token in ("grand final", "final", "semi", "quarter", "round", "match", "playoff", "upper", "lower")):
        score += 3.0
    if label in {"ewc", "rlcs", "playoffs", "swiss", "qualifier"}:
        score -= 2.5
    if "ewc playoffs" in label or "main event" in label:
        score -= 1.0
    score += _team_overlap_score(candidate, label) * 1.8
    score += min(1.2, len(label) / 32.0)
    return score


def _annotate_series_order(candidates: list[dict[str, Any]]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        series_key = candidate.get("group_id") or _series_fallback_key(candidate)
        if series_key:
            grouped.setdefault(series_key, []).append(candidate)
    for grouped_candidates in grouped.values():
        grouped_candidates.sort(
            key=lambda item: (
                item.get("match_date") or item.get("created_at") or datetime.min,
                item["replay_id"],
            )
        )
        for index, candidate in enumerate(grouped_candidates, start=1):
            candidate["series_replay_index"] = index
            candidate["series_replay_count"] = len(grouped_candidates)
            candidate["game_number"] = _extract_game_number(candidate.get("title")) or index
    for candidate in candidates:
        if not candidate.get("series_replay_index"):
            candidate["series_replay_index"] = 1
            candidate["series_replay_count"] = 1
            candidate["game_number"] = _extract_game_number(candidate.get("title")) or 1


def _series_bundles(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        series_key = candidate.get("group_id") or _series_fallback_key(candidate)
        if series_key:
            grouped.setdefault(series_key, []).append(candidate)
    bundles = []
    for bundle_id, replays in grouped.items():
        if len(replays) < 2:
            continue
        ordered = sorted(replays, key=lambda item: (item.get("series_replay_index") or 1, item["replay_id"]))
        series_name = next((item.get("series_name") for item in ordered if item.get("series_name")), None)
        blue = next((item.get("blue_team_name") for item in ordered if item.get("blue_team_name")), None)
        orange = next((item.get("orange_team_name") for item in ordered if item.get("orange_team_name")), None)
        approx_total_duration = sum(float(item.get("duration") or 0.0) for item in ordered)
        if approx_total_duration <= 0:
            approx_total_duration = len(ordered) * 360.0
        bundles.append(
            {
                "bundle_id": bundle_id,
                "series_name": series_name,
                "blue_team_name": blue,
                "orange_team_name": orange,
                "team_names": [name for name in [blue, orange] if name],
                "replays": ordered,
                "approx_total_duration": approx_total_duration,
            }
        )
    return bundles


def _series_fallback_key(candidate: dict[str, Any]) -> str | None:
    series = (candidate.get("series_name") or "").strip().lower()
    blue = (candidate.get("blue_team_name") or "").strip().lower()
    orange = (candidate.get("orange_team_name") or "").strip().lower()
    if series and blue and orange:
        teams = "|".join(sorted([blue, orange]))
        return f"{series}|{teams}"
    return None


def _build_query(candidate: dict[str, Any]) -> str:
    series = candidate.get("series_name")
    blue = candidate.get("blue_team_name")
    orange = candidate.get("orange_team_name")
    title = candidate.get("title")
    if series and blue and orange:
        return f"{series} {blue} vs {orange} Rocket League"
    if blue and orange:
        return f"{blue} vs {orange} Rocket League"
    return f"{title or candidate['replay_id']} Rocket League"


def _build_series_vod_query(bundle: dict[str, Any]) -> str:
    parts: list[str] = []
    if bundle.get("series_name"):
        parts.append(str(bundle["series_name"]))
    teams = [team for team in bundle.get("team_names") or [] if team]
    if len(teams) >= 2:
        parts.append(f"{teams[0]} vs {teams[1]}")
    if not parts:
        return ""
    parts.append("Rocket League VOD")
    return " ".join(parts)


def _published_after(candidate: dict[str, Any]) -> datetime | None:
    date = candidate.get("match_date") or candidate.get("created_at")
    if not isinstance(date, datetime):
        return None
    current = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
    return current - timedelta(days=2)


def _bundle_published_after(bundle: dict[str, Any]) -> datetime | None:
    dates = [item.get("match_date") or item.get("created_at") for item in bundle.get("replays") or []]
    values = [value for value in dates if isinstance(value, datetime)]
    if not values:
        return None
    earliest = min(value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc) for value in values)
    return earliest - timedelta(days=2)


def _score_video(candidate: dict[str, Any], video: dict[str, Any]) -> dict[str, Any]:
    snippet = video.get("snippet") or {}
    title = snippet.get("title") or ""
    description = snippet.get("description") or ""
    haystack = f"{title} {description}".lower()
    score = 0.0
    reasons: list[dict[str, Any]] = []
    score += _text_signals(candidate, haystack, reasons)
    score += _rocket_bonus(haystack, reasons)
    score += _format_bias(candidate, haystack, reasons)
    score += _date_alignment(candidate.get("match_date") or candidate.get("created_at"), snippet.get("publishedAt"), reasons)
    score += _view_bonus(video, reasons)

    duration_iso = (video.get("contentDetails") or {}).get("duration")
    duration_seconds = _parse_iso8601_duration(duration_iso)
    replay_duration = float(candidate.get("duration") or 0.0)
    if duration_seconds and replay_duration > 0:
        ratio = duration_seconds / max(replay_duration, 1.0)
        if 0.55 <= ratio <= 2.5:
            score += 1.4
            reasons.append({"signal": "duration_alignment", "delta": 1.4})
        elif ratio < 0.35:
            score -= 1.2
            reasons.append({"signal": "duration_too_short", "delta": -1.2})
        elif ratio > 8.0:
            score -= 0.9
            reasons.append({"signal": "looks_like_long_vod", "delta": -0.9})

    return _video_item_from_source(
        video=video,
        query_text=_build_query(candidate),
        match_score=score,
        reasons=reasons,
        video_kind="full_video",
        duration_seconds=duration_seconds,
    )


def _score_series_vod(bundle: dict[str, Any], video: dict[str, Any]) -> dict[str, Any]:
    snippet = video.get("snippet") or {}
    title = snippet.get("title") or ""
    description = snippet.get("description") or ""
    haystack = f"{title} {description}".lower()
    score = 0.0
    reasons: list[dict[str, Any]] = []
    score += _text_signals(bundle, haystack, reasons)
    score += _rocket_bonus(haystack, reasons)
    score += _format_bias(bundle, haystack, reasons)
    first_date = (bundle.get("replays") or [{}])[0].get("match_date") or (bundle.get("replays") or [{}])[0].get("created_at")
    score += _date_alignment(first_date, snippet.get("publishedAt"), reasons)
    score += _view_bonus(video, reasons)

    duration_iso = (video.get("contentDetails") or {}).get("duration")
    duration_seconds = _parse_iso8601_duration(duration_iso)
    expected = float(bundle.get("approx_total_duration") or 0.0)
    if duration_seconds and expected > 0:
        ratio = duration_seconds / max(expected, 1.0)
        if ratio >= 0.8:
            score += 2.3
            reasons.append({"signal": "vod_length", "delta": 2.3})
        elif ratio >= 0.5:
            score += 1.0
            reasons.append({"signal": "vod_length_partial", "delta": 1.0})
        else:
            score -= 1.8
            reasons.append({"signal": "vod_too_short", "delta": -1.8})

    chapters = _extract_video_chapters(video, description)
    if chapters:
        game_like = [chapter for chapter in chapters if _is_game_like_label(chapter["label"], bundle)]
        if len(game_like) >= len(bundle.get("replays") or []):
            score += 1.8
            reasons.append({"signal": "chapter_coverage", "delta": 1.8})
        elif game_like:
            score += 0.8
            reasons.append({"signal": "chapter_presence", "delta": 0.8})

    item = _video_item_from_source(
        video=video,
        query_text=_build_series_vod_query(bundle),
        match_score=score,
        reasons=reasons,
        video_kind="full_video",
        duration_seconds=duration_seconds,
    )
    item["chapters"] = chapters
    return item


def _assign_bundle_segments(bundle: dict[str, Any], vod: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    chapters = vod.get("chapters") or []
    video_duration = float(vod.get("duration_seconds") or 0.0) or None
    windows = _chapter_windows(chapters, video_duration)
    game_windows = [window for window in windows if _is_game_like_label(window["label"], bundle)]
    if not game_windows:
        return {}

    explicit_numbers = {window.get("game_number"): window for window in game_windows if window.get("game_number")}
    sequential = game_windows[:]
    assignments: dict[str, list[dict[str, Any]]] = {}
    used_windows: set[int] = set()

    for replay in bundle.get("replays") or []:
        replay_number = replay.get("game_number") or replay.get("series_replay_index") or 1
        candidate_windows: list[tuple[float, dict[str, Any]]] = []
        if replay_number in explicit_numbers:
            candidate_windows.append((_chapter_assignment_score(replay, bundle, explicit_numbers[replay_number], replay_number), explicit_numbers[replay_number]))
        for order_index, window in enumerate(sequential, start=1):
            if id(window) in used_windows and window.get("game_number") != replay_number:
                continue
            candidate_windows.append((_chapter_assignment_score(replay, bundle, window, order_index), window))
        if not candidate_windows:
            continue
        score, window = max(candidate_windows, key=lambda item: item[0])
        if score < 3.0:
            continue
        used_windows.add(id(window))
        assignments[replay["replay_id"]] = [
            _build_segment_item(replay, vod, window, bundle["bundle_id"], score)
        ]
    return assignments


def _assign_bundle_video_items(bundle: dict[str, Any], vod: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    assignments = _assign_bundle_segments(bundle, vod) if vod.get("chapters") else {}
    missing = {
        replay["replay_id"]
        for replay in bundle.get("replays") or []
        if replay.get("replay_id") not in assignments
    }
    if missing:
        estimates = _assign_bundle_estimates(bundle, vod, missing_replay_ids=missing)
        for replay_id, items in estimates.items():
            assignments.setdefault(replay_id, []).extend(items)
    return assignments


def _chapter_windows(chapters: list[dict[str, Any]], video_duration: float | None) -> list[dict[str, Any]]:
    output = []
    for index, chapter in enumerate(chapters):
        next_start = chapters[index + 1]["start_seconds"] if index + 1 < len(chapters) else None
        end_seconds = next_start if next_start is not None else video_duration
        output.append(
            {
                **chapter,
                "end_seconds": end_seconds,
                "window_seconds": (end_seconds - chapter["start_seconds"]) if end_seconds is not None else None,
            }
        )
    return output


def _chapter_assignment_score(
    replay: dict[str, Any],
    bundle: dict[str, Any],
    window: dict[str, Any],
    order_index: int,
) -> float:
    score = 1.0
    label = (window.get("label") or "").lower()
    replay_number = replay.get("game_number") or replay.get("series_replay_index") or 1
    if window.get("game_number") == replay_number:
        score += 3.6
    elif window.get("game_number") and window.get("game_number") != replay_number:
        score -= 2.0
    if order_index == replay.get("series_replay_index"):
        score += 1.6

    score += _team_overlap_score(replay, label)
    if bundle.get("series_name"):
        overlap = _token_overlap(str(bundle["series_name"]), label)
        if overlap > 0:
            score += overlap * 1.2

    replay_duration = float(replay.get("duration") or 0.0)
    window_seconds = float(window.get("window_seconds") or 0.0)
    if replay_duration > 0 and window_seconds > 0:
        if window_seconds >= replay_duration * 0.8:
            delta = max(-1.0, 1.8 - min(abs(window_seconds - replay_duration) / 150.0, 2.4))
            score += delta
        else:
            score -= 1.5
    if any(token in label for token in NON_GAME_WORDS):
        score -= 4.0
    return score


def _build_segment_item(
    replay: dict[str, Any],
    vod: dict[str, Any],
    window: dict[str, Any],
    bundle_id: str,
    chapter_score: float,
) -> dict[str, Any]:
    source_video_id = vod["source_video_id"]
    start_seconds = max(0, int(window["start_seconds"]) - 8)
    end_seconds = _segment_end_seconds(replay, window)
    segment_label = window.get("label") or f"Game {replay.get('series_replay_index') or 1}"
    segment_id = f"{source_video_id}#clip-{start_seconds}-{end_seconds or 'end'}"
    reasons = list(vod.get("reasons") or [])
    reasons.append({"signal": "chapter_match", "delta": round(chapter_score, 3), "label": segment_label})
    reasons.append({"signal": "bundle", "delta": 0.0, "value": bundle_id})
    match_score = round(float(vod["match_score"]) + chapter_score, 4)
    return {
        **vod,
        "video_id": segment_id,
        "source_video_id": source_video_id,
        "query_text": vod["query_text"],
        "embed_url": _build_embed_url(source_video_id, start_seconds, end_seconds),
        "watch_url": _build_watch_url(source_video_id, start_seconds, end_seconds),
        "match_score": match_score,
        "video_kind": "vod_segment",
        "segment_label": segment_label,
        "segment_start_seconds": start_seconds,
        "segment_end_seconds": end_seconds,
        "segment_confidence": round(min(1.0, max(0.0, chapter_score / 8.0)), 4),
        "duration_seconds": (end_seconds - start_seconds) if end_seconds else vod.get("duration_seconds"),
        "reasons": reasons,
    }


def _assign_bundle_estimates(
    bundle: dict[str, Any],
    vod: dict[str, Any],
    *,
    missing_replay_ids: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    video_duration = float(vod.get("duration_seconds") or 0.0)
    replays = list(bundle.get("replays") or [])
    if video_duration <= 0 or len(replays) < 2:
        return {}
    approx_total = float(bundle.get("approx_total_duration") or 0.0)
    if approx_total <= 0:
        approx_total = sum(max(180.0, float(replay.get("duration") or 0.0)) for replay in replays)
    if approx_total <= 0:
        return {}
    ratio = video_duration / max(approx_total, 1.0)
    if ratio < 0.7 or ratio > MAX_SERIES_VOD_ESTIMATE_RATIO:
        return {}

    timeline = _estimate_bundle_timeline(bundle, vod)
    if not timeline:
        return {}

    allowed = set(missing_replay_ids or [])
    assignments: dict[str, list[dict[str, Any]]] = {}
    for slot in timeline:
        replay = slot["replay"]
        replay_id = replay["replay_id"]
        if allowed and replay_id not in allowed:
            continue
        estimate_score = _estimated_segment_score(bundle, vod, slot)
        if estimate_score < MIN_ESTIMATED_SEGMENT_SCORE:
            continue
        assignments[replay_id] = [_build_estimated_segment_item(replay, vod, bundle["bundle_id"], slot, estimate_score, ratio)]
    return assignments


def _estimate_bundle_timeline(bundle: dict[str, Any], vod: dict[str, Any]) -> list[dict[str, Any]]:
    replays = list(bundle.get("replays") or [])
    if not replays:
        return []
    durations = [max(180.0, float(replay.get("duration") or 0.0)) for replay in replays]
    video_duration = float(vod.get("duration_seconds") or 0.0)
    if video_duration <= 0:
        return []

    n = len(replays)
    total_game_seconds = sum(durations)
    if total_game_seconds <= 0:
        return []
    overhead = max(0.0, video_duration - total_game_seconds)
    pre_roll = min(150.0, max(24.0, overhead * 0.16)) if overhead > 0 else 12.0
    between_gaps: list[float] = []
    if n > 1:
        distributable = max(0.0, overhead - pre_roll)
        base_gap = distributable / (n - 1) if n > 1 else 0.0
        between_gaps = [min(165.0, max(22.0, base_gap)) for _ in range(n - 1)]
        consumed = pre_roll + sum(between_gaps)
        if consumed > overhead:
            scale = max(0.0, (overhead - pre_roll) / max(sum(between_gaps), 1.0))
            between_gaps = [gap * scale for gap in between_gaps]
    current = pre_roll
    timeline: list[dict[str, Any]] = []
    for index, replay in enumerate(replays):
        duration = durations[index]
        window_start = max(0, int(round(current)))
        next_start = current + duration
        if index < len(between_gaps):
            next_start += between_gaps[index]
        window_end = int(round(next_start))
        timeline.append(
            {
                "replay": replay,
                "game_number": replay.get("game_number") or replay.get("series_replay_index") or index + 1,
                "start_seconds": window_start,
                "end_seconds": max(window_start + 60, window_end),
                "duration_seconds": duration,
            }
        )
        current = next_start
    return timeline


def _estimated_segment_score(bundle: dict[str, Any], vod: dict[str, Any], slot: dict[str, Any]) -> float:
    replay = slot["replay"]
    video_duration = float(vod.get("duration_seconds") or 0.0)
    expected = float(bundle.get("approx_total_duration") or 0.0)
    ratio = video_duration / max(expected, 1.0) if expected > 0 else 1.0
    score = 2.2
    score += _team_overlap_score(replay, f"{vod.get('title', '')} {vod.get('description', '')}".lower()) * 0.55
    if 0.85 <= ratio <= 2.4:
        score += 1.2
    elif ratio <= MAX_SERIES_VOD_ESTIMATE_RATIO:
        score += max(0.2, 1.1 - (ratio - 2.4) * 0.35)
    replay_duration = float(replay.get("duration") or 0.0)
    if replay_duration > 0:
        estimated_window = max(1.0, float(slot["end_seconds"] - slot["start_seconds"]))
        duration_gap = abs(estimated_window - replay_duration)
        score += max(-0.9, 1.0 - min(duration_gap / 240.0, 1.9))
    return round(score, 4)


def _build_estimated_segment_item(
    replay: dict[str, Any],
    vod: dict[str, Any],
    bundle_id: str,
    slot: dict[str, Any],
    estimate_score: float,
    ratio: float,
) -> dict[str, Any]:
    source_video_id = vod["source_video_id"]
    replay_duration = float(replay.get("duration") or 0.0)
    start_seconds = max(0, int(slot["start_seconds"]) - 10)
    if replay_duration > 0:
        target_end = start_seconds + replay_duration + 90
    else:
        target_end = int(slot["end_seconds"])
    end_seconds = max(start_seconds + 60, min(int(slot["end_seconds"]), int(round(target_end))))
    game_number = slot.get("game_number") or replay.get("series_replay_index") or 1
    segment_label = f"Estimated Game {game_number}"
    segment_id = f"{source_video_id}#estimate-{start_seconds}-{end_seconds}"
    reasons = list(vod.get("reasons") or [])
    reasons.append({"signal": "estimated_series_window", "delta": round(estimate_score, 3), "label": segment_label})
    reasons.append({"signal": "bundle", "delta": 0.0, "value": bundle_id})
    reasons.append({"signal": "vod_ratio", "delta": 0.0, "value": round(ratio, 3)})
    match_score = round(float(vod["match_score"]) + estimate_score, 4)
    return {
        **vod,
        "video_id": segment_id,
        "source_video_id": source_video_id,
        "query_text": vod["query_text"],
        "embed_url": _build_embed_url(source_video_id, start_seconds, end_seconds),
        "watch_url": _build_watch_url(source_video_id, start_seconds, end_seconds),
        "match_score": match_score,
        "video_kind": "vod_estimate",
        "segment_label": segment_label,
        "segment_start_seconds": start_seconds,
        "segment_end_seconds": end_seconds,
        "segment_confidence": round(min(0.74, max(0.28, estimate_score / 8.5)), 4),
        "duration_seconds": end_seconds - start_seconds,
        "reasons": reasons,
    }


def _segment_end_seconds(replay: dict[str, Any], window: dict[str, Any]) -> int | None:
    start_seconds = int(window["start_seconds"])
    next_boundary = int(window["end_seconds"]) if window.get("end_seconds") else None
    replay_duration = float(replay.get("duration") or 0.0)
    if replay_duration <= 0:
        return max(start_seconds + 60, (next_boundary - 4) if next_boundary else start_seconds + 420)
    padded_target = int(round(start_seconds + replay_duration + 75))
    if next_boundary is None:
        return padded_target
    if next_boundary - start_seconds > replay_duration + 240:
        return max(start_seconds + 60, min(next_boundary - 4, padded_target))
    return max(start_seconds + 60, next_boundary - 4)


def _dedupe_video_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        existing = best_by_id.get(item["video_id"])
        if existing is None or float(item.get("match_score") or 0.0) > float(existing.get("match_score") or 0.0):
            best_by_id[item["video_id"]] = item
    return sorted(
        best_by_id.values(),
        key=lambda item: (
            float(item.get("segment_confidence") or 0.0),
            2 if item.get("video_kind") == "vod_segment" else 1 if item.get("video_kind") == "vod_estimate" else 0,
            float(item.get("match_score") or 0.0),
            item.get("published_at") or "",
        ),
        reverse=True,
    )


def _video_item_from_source(
    *,
    video: dict[str, Any],
    query_text: str,
    match_score: float,
    reasons: list[dict[str, Any]],
    video_kind: str,
    duration_seconds: float | None,
) -> dict[str, Any]:
    snippet = video.get("snippet") or {}
    thumbnails = snippet.get("thumbnails") or {}
    thumb = (thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}).get("url")
    source_video_id = video.get("id")
    title = snippet.get("title") or ""
    description = snippet.get("description") or ""
    return {
        "video_id": source_video_id,
        "source_video_id": source_video_id,
        "title": title,
        "channel_title": snippet.get("channelTitle"),
        "published_at": snippet.get("publishedAt"),
        "description": description,
        "thumbnail_url": thumb,
        "duration_iso8601": (video.get("contentDetails") or {}).get("duration"),
        "duration_seconds": duration_seconds,
        "view_count": int((video.get("statistics") or {}).get("viewCount") or 0),
        "embed_url": _build_embed_url(source_video_id, None, None),
        "watch_url": _build_watch_url(source_video_id, None, None),
        "query_text": query_text,
        "match_score": round(match_score, 4),
        "reasons": reasons,
        "video_kind": video_kind,
        "segment_label": None,
        "segment_start_seconds": None,
        "segment_end_seconds": None,
        "segment_confidence": None,
    }


def _build_embed_url(video_id: str, start_seconds: int | None, end_seconds: int | None) -> str:
    query = ["rel=0", "playsinline=1"]
    if start_seconds is not None:
        query.append(f"start={start_seconds}")
    if end_seconds is not None:
        query.append(f"end={end_seconds}")
    return f"https://www.youtube.com/embed/{video_id}?{'&'.join(query)}"


def _build_watch_url(video_id: str, start_seconds: int | None, end_seconds: int | None) -> str:
    query = [f"v={video_id}"]
    if start_seconds is not None:
        query.append(f"t={start_seconds}s")
    if end_seconds is not None:
        query.append(f"end={end_seconds}")
    return f"https://www.youtube.com/watch?{'&'.join(query)}"


def _text_signals(candidate: dict[str, Any], haystack: str, reasons: list[dict[str, Any]]) -> float:
    score = 0.0
    team_hits = 0
    team_fields = 0
    for label, text, weight in (
        ("blue_team", candidate.get("blue_team_name"), 3.0),
        ("orange_team", candidate.get("orange_team_name"), 3.0),
        ("series", candidate.get("series_name"), 2.0),
    ):
        normalized = (text or "").strip().lower()
        if not normalized:
            continue
        if label in {"blue_team", "orange_team"}:
            team_fields += 1
        if normalized in haystack:
            score += weight
            reasons.append({"signal": label, "delta": weight})
            if label in {"blue_team", "orange_team"}:
                team_hits += 1
            continue
        overlap = _token_overlap(normalized, haystack)
        if overlap > 0:
            delta = overlap * weight
            score += delta
            reasons.append({"signal": f"{label}_tokens", "delta": round(delta, 3)})
            if label in {"blue_team", "orange_team"} and overlap >= 0.5:
                team_hits += 1
    if team_fields >= 2:
        if team_hits == 2:
            score += 1.8
            reasons.append({"signal": "exact_matchup", "delta": 1.8})
        elif team_hits == 1:
            score -= 7.6
            reasons.append({"signal": "partial_matchup", "delta": -7.6})
        elif " vs " in haystack or " vs. " in haystack:
            score -= 3.0
            reasons.append({"signal": "missing_matchup", "delta": -3.0})
    return score


def _format_bias(candidate: dict[str, Any], haystack: str, reasons: list[dict[str, Any]]) -> float:
    score = 0.0
    for token, delta in BAD_VIDEO_TOKENS:
        if token in haystack:
            score += delta
            reasons.append({"signal": "format_penalty", "delta": delta, "token": token})
    for token, delta in GOOD_VIDEO_TOKENS:
        if token in haystack:
            score += delta
            reasons.append({"signal": "format_bonus", "delta": delta, "token": token})
    replay_duration = float(candidate.get("duration") or 0.0)
    if replay_duration > 0 and any(token in haystack for token, _ in BAD_VIDEO_TOKENS):
        score -= 0.8
        reasons.append({"signal": "short_form_bias", "delta": -0.8})
    return score


def _rocket_bonus(haystack: str, reasons: list[dict[str, Any]]) -> float:
    rocket_bonus = 0.0
    if "rocket league" in haystack:
        rocket_bonus += 1.4
    if "rlcs" in haystack:
        rocket_bonus += 1.0
    if rocket_bonus:
        reasons.append({"signal": "rocket_league_context", "delta": rocket_bonus})
    return rocket_bonus


def _date_alignment(match_date: Any, published_at_text: str | None, reasons: list[dict[str, Any]]) -> float:
    published_at = _parse_dt(published_at_text)
    if not published_at or not isinstance(match_date, datetime):
        return 0.0
    current = match_date.replace(tzinfo=timezone.utc) if match_date.tzinfo is None else match_date.astimezone(timezone.utc)
    day_gap = abs((published_at - current).total_seconds()) / 86400.0
    delta = max(-2.0, 2.0 - min(day_gap / 7.0, 4.0))
    reasons.append({"signal": "date_alignment", "delta": round(delta, 3)})
    return delta


def _view_bonus(video: dict[str, Any], reasons: list[dict[str, Any]]) -> float:
    views = int((video.get("statistics") or {}).get("viewCount") or 0)
    if views <= 0:
        return 0.0
    delta = min(1.0, math.log10(views + 1) / 10.0)
    reasons.append({"signal": "views", "delta": round(delta, 3)})
    return delta


def _parse_chapters(description: str | None) -> list[dict[str, Any]]:
    if not description:
        return []
    rows: list[dict[str, Any]] = []
    seen_starts: set[int] = set()
    for raw_line in re.split(r"[\r\n]+", description):
        line = raw_line.strip()
        if not line or "http://" in line or "https://" in line:
            continue
        match = CHAPTER_PREFIX_PATTERN.match(line) or CHAPTER_SUFFIX_PATTERN.match(line)
        if not match:
            continue
        ts = match.group("ts")
        label = match.group("label").strip(" -|")
        if not label:
            continue
        start_seconds = _timestamp_seconds(ts)
        if start_seconds is None or start_seconds in seen_starts:
            continue
        seen_starts.add(start_seconds)
        rows.append(
            {
                "start_seconds": start_seconds,
                "label": label,
                "game_number": _extract_game_number(label),
            }
        )
    rows.sort(key=lambda item: item["start_seconds"])
    return rows


def _timestamp_seconds(text: str | None) -> int | None:
    if not text:
        return None
    parts = [int(piece) for piece in text.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    return None


def _parse_iso8601_duration(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(
        r"PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        value,
    )
    if not match:
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return float(hours * 3600 + minutes * 60 + seconds)


def _seconds_to_iso8601(value: float | int | None) -> str | None:
    if value is None:
        return None
    total = max(0, int(round(float(value))))
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    pieces = ["PT"]
    if hours:
        pieces.append(f"{hours}H")
    if minutes:
        pieces.append(f"{minutes}M")
    if seconds or len(pieces) == 1:
        pieces.append(f"{seconds}S")
    return "".join(pieces)


def _extract_game_number(text: str | None) -> int | None:
    if not text:
        return None
    match = GAME_NUMBER_PATTERN.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _is_game_like_label(label: str, bundle: dict[str, Any] | None = None) -> bool:
    lowered = (label or "").lower()
    if any(token in lowered for token in NON_GAME_WORDS):
        return False
    if any(token in lowered for token in GAME_WORDS):
        return True
    if bundle:
        for team_name in bundle.get("team_names") or []:
            if _token_overlap(team_name, lowered) >= 0.5:
                return True
    return False


def _team_overlap_score(replay: dict[str, Any], label: str) -> float:
    score = 0.0
    for team_name in (replay.get("blue_team_name"), replay.get("orange_team_name")):
        overlap = _token_overlap(team_name or "", label)
        if overlap > 0:
            score += overlap * 2.2
    return score


def _token_overlap(needle: str, haystack: str) -> float:
    tokens = {token for token in re.split(r"[^a-z0-9]+", needle.lower()) if len(token) > 2}
    if not tokens:
        return 0.0
    matched = sum(1 for token in tokens if token in haystack)
    return matched / len(tokens)


def _normalize_ytdlp_video(info: dict[str, Any]) -> dict[str, Any]:
    video_id = info.get("id")
    duration_seconds = float(info.get("duration") or 0.0) or None
    thumbnail_url = info.get("thumbnail")
    if not thumbnail_url:
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumbnail_url = thumbs[-1].get("url")
    return {
        "id": video_id,
        "snippet": {
            "title": info.get("title") or "",
            "description": info.get("description") or "",
            "publishedAt": _isoformat_or_none(_yt_dlp_published_at(info)),
            "channelTitle": info.get("channel") or info.get("uploader") or "",
            "thumbnails": {
                "high": {"url": thumbnail_url},
            } if thumbnail_url else {},
        },
        "contentDetails": {
            "duration": _seconds_to_iso8601(duration_seconds),
        },
        "statistics": {
            "viewCount": int(info.get("view_count") or 0),
        },
        "chapters": _ytdlp_chapters(info),
    }


def _ytdlp_chapters(info: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen_starts: set[int] = set()
    for chapter in info.get("chapters") or []:
        label = (chapter.get("title") or "").strip()
        start_seconds = chapter.get("start_time")
        if not label or start_seconds is None:
            continue
        start_value = int(round(float(start_seconds)))
        if start_value in seen_starts:
            continue
        seen_starts.add(start_value)
        output.append(
            {
                "start_seconds": start_value,
                "label": label,
                "game_number": _extract_game_number(label),
            }
        )
    output.sort(key=lambda item: item["start_seconds"])
    return output


def _extract_video_chapters(video: dict[str, Any], description: str | None) -> list[dict[str, Any]]:
    chapters = video.get("chapters") or []
    if chapters:
        return chapters
    return _parse_chapters(description)


def _yt_dlp_published_at(info: dict[str, Any]) -> datetime | None:
    timestamp = info.get("timestamp") or info.get("release_timestamp")
    if timestamp:
        try:
            return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    upload_date = info.get("upload_date")
    if upload_date and re.fullmatch(r"\d{8}", str(upload_date)):
        try:
            parsed = datetime.strptime(str(upload_date), "%Y%m%d")
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    release_date = info.get("release_date")
    if release_date and re.fullmatch(r"\d{8}", str(release_date)):
        try:
            parsed = datetime.strptime(str(release_date), "%Y%m%d")
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _isoformat_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
