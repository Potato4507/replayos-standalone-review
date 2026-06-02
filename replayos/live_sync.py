from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import duckdb

from .config import get_settings
from .db import rows_to_dicts


RLCS_KEYWORDS = (
    "rlcs",
    "major",
    "world championship",
    "open ",
    "playoffs",
    "watch party",
    "drops",
)
LANGUAGE_CODES = {"ENG", "FR", "ESP", "DE", "PT", "IT", "NL", "AR", "TR", "JP", "KR"}
DAY_PATTERN = re.compile(r"^[A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2}$")
TIME_PATTERN = re.compile(r"^\d{1,2}:\d{2}$")
BEST_OF_PATTERN = re.compile(r"^BO\d+$", re.IGNORECASE)
VIEWER_PATTERN = re.compile(r"([0-9][0-9,]*)\s+viewers", re.IGNORECASE)
RANK_PATTERN = re.compile(r"^#(\d+)$")
POINTS_PATTERN = re.compile(r"^(\d+)\s+pts$")
LIVE_SYNC_LOCK = threading.Lock()


class LiveSyncError(RuntimeError):
    """Raised when the live schedule or stream sources fail."""


@dataclass
class LiveSyncResult:
    run_id: str
    schedule_updated: bool
    streams_updated: bool
    tournaments_seen: int
    matches_seen: int
    streams_seen: int
    stale_before_sync: bool


class _TokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tokens: list[str] = []

    def handle_data(self, data: str) -> None:
        normalized = re.sub(r"\s+", " ", unescape(data or "")).strip()
        if normalized:
            self.tokens.append(normalized)


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        self._href = href or None
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is None:
            return
        normalized = re.sub(r"\s+", " ", unescape(data or "")).strip()
        if normalized:
            self._parts.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        text = " ".join(self._parts).strip()
        if text:
            self.links.append({"href": self._href, "text": text})
        self._href = None
        self._parts = []


def ensure_live_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS live_sync_runs (
            run_id VARCHAR PRIMARY KEY,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            result_json VARCHAR,
            status VARCHAR,
            error VARCHAR
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_live_sync_runs_started_at ON live_sync_runs (started_at)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS live_tournaments (
            tournament_slug VARCHAR PRIMARY KEY,
            name VARCHAR,
            status VARCHAR,
            start_at TIMESTAMP,
            end_at TIMESTAMP,
            location_name VARCHAR,
            prize_pool VARCHAR,
            page_url VARCHAR,
            description VARCHAR,
            updated_at TIMESTAMP,
            raw_json VARCHAR
        )
        """
    )
    _ensure_live_watch_channels_table(con)
    _ensure_live_matches_table(con)
    _ensure_live_streams_table(con)
    _ensure_live_leaderboards_table(con)
    con.execute("CREATE INDEX IF NOT EXISTS idx_live_tournaments_status_start ON live_tournaments (status, start_at)")


def _ensure_live_watch_channels_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS live_watch_channels (
            tournament_slug VARCHAR,
            language VARCHAR,
            channel_name VARCHAR,
            channel_url VARCHAR,
            official BOOLEAN,
            updated_at TIMESTAMP,
            PRIMARY KEY (tournament_slug, language, channel_name)
        )
        """
    )


def _ensure_live_matches_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS live_matches (
            match_id VARCHAR PRIMARY KEY,
            tournament_slug VARCHAR,
            day_label VARCHAR,
            stage VARCHAR,
            match_label VARCHAR,
            best_of VARCHAR,
            scheduled_label VARCHAR,
            scheduled_at TIMESTAMP,
            team_a VARCHAR,
            team_b VARCHAR,
            status VARCHAR,
            match_url VARCHAR,
            series_score_a BIGINT,
            series_score_b BIGINT,
            game_scores_json VARCHAR,
            sort_order BIGINT,
            updated_at TIMESTAMP
        )
        """
    )
    _ensure_columns(
        con,
        "live_matches",
        {
            "match_url": "VARCHAR",
            "series_score_a": "BIGINT",
            "series_score_b": "BIGINT",
            "game_scores_json": "VARCHAR",
        },
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_live_matches_tournament_sort ON live_matches (tournament_slug, sort_order)")


def _ensure_live_streams_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS live_streams (
            channel_name VARCHAR PRIMARY KEY,
            title VARCHAR,
            author_name VARCHAR,
            platform VARCHAR,
            viewer_count BIGINT,
            stream_url VARCHAR,
            embed_url VARCHAR,
            thumbnail_url VARCHAR,
            started_at TIMESTAMP,
            is_live BOOLEAN,
            classification VARCHAR,
            tournament_slug VARCHAR,
            updated_at TIMESTAMP,
            raw_json VARCHAR
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_live_streams_classification_viewers ON live_streams (classification, viewer_count)")


def _ensure_live_leaderboards_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS live_leaderboards (
            board_key VARCHAR,
            board_name VARCHAR,
            stage_key VARCHAR,
            region VARCHAR,
            rank BIGINT,
            team_name VARCHAR,
            points BIGINT,
            players_json VARCHAR,
            source_url VARCHAR,
            updated_at TIMESTAMP,
            PRIMARY KEY (board_key, rank, team_name)
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_live_leaderboards_board_region_rank ON live_leaderboards (board_key, region, rank)")


def _recreate_live_schedule_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("DROP TABLE IF EXISTS live_watch_channels")
    con.execute("DROP TABLE IF EXISTS live_matches")
    con.execute("DROP TABLE IF EXISTS live_leaderboards")
    _ensure_live_watch_channels_table(con)
    _ensure_live_matches_table(con)
    _ensure_live_leaderboards_table(con)


def _recreate_live_streams_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("DROP TABLE IF EXISTS live_streams")
    _ensure_live_streams_table(con)


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


def prune_live_sync_runs(con: duckdb.DuckDBPyConnection) -> None:
    keep_runs = max(10, int(get_settings().sync_run_retention))
    count = con.execute("SELECT COUNT(*) FROM live_sync_runs").fetchone()[0]
    if int(count or 0) <= keep_runs:
        return
    overflow = int(count) - keep_runs
    con.execute(
        """
        DELETE FROM live_sync_runs
        WHERE run_id IN (
            SELECT run_id
            FROM live_sync_runs
            ORDER BY started_at ASC NULLS FIRST
            LIMIT ?
        )
        """,
        [overflow],
    )


def sync_live_data(
    serving_db: Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    serving_db = Path(serving_db or settings.serving_db)
    now = datetime.now(timezone.utc)
    run_id = now.strftime("live_sync_%Y%m%dT%H%M%S%fZ")
    lock_acquired = LIVE_SYNC_LOCK.acquire(blocking=force)
    if not lock_acquired:
        return {
            "run_id": run_id,
            "skipped": True,
            "busy": True,
            "schedule_updated": False,
            "streams_updated": False,
            "stale_before_sync": False,
        }

    con = duckdb.connect(str(serving_db))
    in_transaction = False
    try:
        ensure_live_schema(con)
        stale = _stale_snapshot(con, "live_streams", settings.live_stream_cache_seconds) or _stale_snapshot(
            con, "live_tournaments", settings.live_schedule_cache_seconds
        )
        schedule_due = force or _stale_snapshot(con, "live_tournaments", settings.live_schedule_cache_seconds)
        streams_due = force or _stale_snapshot(con, "live_streams", settings.live_stream_cache_seconds)
        if not schedule_due and not streams_due:
            return {
                "run_id": run_id,
                "skipped": True,
                "schedule_updated": False,
                "streams_updated": False,
                "stale_before_sync": stale,
            }

        con.execute(
            "INSERT OR REPLACE INTO live_sync_runs VALUES (?, ?, ?, ?, ?, ?)",
            [run_id, now, None, None, "running", None],
        )
        con.execute("BEGIN TRANSACTION")
        in_transaction = True

        tournaments_seen = 0
        matches_seen = 0
        streams_seen = 0

        if schedule_due:
            tournament_html = _fetch_html(settings.blast_rl_tournaments_url)
            tournaments = _parse_blast_tournament_index(tournament_html, now)
            tournaments_seen = len(tournaments)
            for tournament in tournaments:
                con.execute(
                    """
                    INSERT OR REPLACE INTO live_tournaments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        tournament["tournament_slug"],
                        tournament["name"],
                        tournament["status"],
                        tournament["start_at"],
                        tournament["end_at"],
                        tournament["location_name"],
                        tournament["prize_pool"],
                        tournament["page_url"],
                        tournament["description"],
                        now,
                        _json(tournament["raw"]),
                    ],
                )

            focus = [row for row in tournaments if row["status"] in {"live", "upcoming"}][:5]
            _recreate_live_schedule_tables(con)
            schedule_channels: list[dict[str, Any]] = []
            schedule_matches: list[dict[str, Any]] = []
            for tournament in focus:
                root_html = _fetch_html(tournament["page_url"])
                tournament.update(_parse_tournament_page(root_html, now))
                con.execute(
                    """
                    INSERT OR REPLACE INTO live_tournaments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        tournament["tournament_slug"],
                        tournament["name"],
                        tournament["status"],
                        tournament["start_at"],
                        tournament["end_at"],
                        tournament["location_name"],
                        tournament["prize_pool"],
                        tournament["page_url"],
                        tournament["description"],
                        now,
                        _json(tournament["raw"]),
                    ],
                )
                series_html = _fetch_html(f"{tournament['page_url'].rstrip('/')}/series")
                channels = _parse_watch_channels(series_html, tournament["tournament_slug"], now)
                matches = _parse_blast_matches(
                    series_html,
                    tournament["tournament_slug"],
                    now,
                    base_url=tournament["page_url"],
                )
                for match in matches[:12]:
                    if not match.get("match_url"):
                        continue
                    try:
                        detail_html = _fetch_html(match["match_url"])
                        match.update(_parse_series_match_page(detail_html, match))
                    except Exception:
                        continue
                schedule_channels.extend(channels)
                schedule_matches.extend(matches)
            seen_channels: set[tuple[str, str, str]] = set()
            for channel in schedule_channels:
                key = (
                    channel["tournament_slug"],
                    channel["language"],
                    channel["channel_name"].lower(),
                )
                if key in seen_channels:
                    continue
                seen_channels.add(key)
                con.execute(
                    "INSERT INTO live_watch_channels VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        channel["tournament_slug"],
                        channel["language"],
                        channel["channel_name"],
                        channel["channel_url"],
                        channel["official"],
                        now,
                    ],
                )
            for match in schedule_matches:
                con.execute(
                    """
                    INSERT INTO live_matches (
                        match_id, tournament_slug, day_label, stage, match_label, best_of,
                        scheduled_label, scheduled_at, team_a, team_b, status, sort_order, updated_at,
                        match_url, series_score_a, series_score_b, game_scores_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        match["match_id"],
                        match["tournament_slug"],
                        match["day_label"],
                        match["stage"],
                        match["match_label"],
                        match["best_of"],
                        match["scheduled_label"],
                        match["scheduled_at"],
                        match["team_a"],
                        match["team_b"],
                        match["status"],
                        match["sort_order"],
                        now,
                        match.get("match_url"),
                        match.get("series_score_a"),
                        match.get("series_score_b"),
                        _json(match.get("games") or []),
                    ],
                )
            matches_seen = len(schedule_matches)

            context = _leaderboard_context_from_tournaments(focus)
            if context:
                for board in _fetch_leaderboards(context, now):
                    con.executemany(
                        "INSERT INTO live_leaderboards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            [
                                board["board_key"],
                                board["board_name"],
                                board["stage_key"],
                                board["region"],
                                row["rank"],
                                row["team_name"],
                                row["points"],
                                _json(row.get("players") or []),
                                board["source_url"],
                                now,
                            ]
                            for row in board["items"]
                        ],
                    )

        if streams_due:
            official_channels = {
                row[0].lower()
                for row in con.execute("SELECT DISTINCT channel_name FROM live_watch_channels").fetchall()
                if row[0]
            }
            known_players = _known_names(con, "remote_players", "player_name")
            known_teams = _known_team_names(con)
            tournaments = rows_to_dicts(
                con.execute(
                    """
                    SELECT tournament_slug, name
                    FROM live_tournaments
                    WHERE status IN ('live', 'upcoming')
                    ORDER BY CASE status WHEN 'live' THEN 0 ELSE 1 END, start_at
                    LIMIT 8
                    """
                )
            )
            stream_html = _fetch_html(settings.rocketleague_tv_url)
            streams = _parse_rocketleague_tv_streams(
                stream_html,
                official_channels=official_channels,
                known_players=known_players,
                known_teams=known_teams,
                tournaments=tournaments,
                fetched_at=now,
            )
            streams_seen = len(streams)
            _recreate_live_streams_table(con)
            inserted_channels: set[str] = set()
            for stream in streams:
                channel_key = (stream.get("channel_name") or "").lower()
                if not channel_key or channel_key in inserted_channels:
                    continue
                inserted_channels.add(channel_key)
                con.execute(
                    """
                    INSERT INTO live_streams VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        stream["channel_name"],
                        stream["title"],
                        stream["author_name"],
                        stream["platform"],
                        stream["viewer_count"],
                        stream["stream_url"],
                        stream["embed_url"],
                        stream["thumbnail_url"],
                        stream["started_at"],
                        stream["is_live"],
                        stream["classification"],
                        stream["tournament_slug"],
                        now,
                        _json(stream["raw"]),
                    ],
                )

        result = LiveSyncResult(
            run_id=run_id,
            schedule_updated=schedule_due,
            streams_updated=streams_due,
            tournaments_seen=tournaments_seen,
            matches_seen=matches_seen,
            streams_seen=streams_seen,
            stale_before_sync=stale,
        )
        completed_at = datetime.now(timezone.utc)
        con.execute(
            "INSERT OR REPLACE INTO live_sync_runs VALUES (?, ?, ?, ?, ?, ?)",
            [run_id, now, completed_at, _json(result.__dict__), "completed", None],
        )
        prune_live_sync_runs(con)
        con.execute("COMMIT")
        in_transaction = False
        return {**result.__dict__, "completed_at": completed_at.isoformat(), "skipped": False}
    except Exception as exc:
        completed_at = datetime.now(timezone.utc)
        if in_transaction:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
        try:
            failure_con = duckdb.connect(str(serving_db))
            try:
                ensure_live_schema(failure_con)
                failure_con.execute(
                    "INSERT OR REPLACE INTO live_sync_runs VALUES (?, ?, ?, ?, ?, ?)",
                    [run_id, now, completed_at, None, "failed", str(exc)],
                )
                prune_live_sync_runs(failure_con)
            finally:
                failure_con.close()
        except Exception:
            pass
        raise LiveSyncError(str(exc)) from exc
    finally:
        con.close()
        LIVE_SYNC_LOCK.release()


def live_status(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    if not _table_exists(con, "live_sync_runs"):
        return {
            "configured_tables": False,
            "streams": 0,
            "tournaments": 0,
            "matches": 0,
            "last_run": None,
        }
    last = rows_to_dicts(
        con.execute(
            """
            SELECT run_id, started_at, completed_at, status, error, result_json
            FROM live_sync_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        )
    )
    latest = last[0] if last else None
    if latest and latest.get("result_json"):
        latest["result"] = json.loads(latest.pop("result_json"))
    return {
        "configured_tables": True,
        "streams": _count(con, "live_streams"),
        "tournaments": _count(con, "live_tournaments"),
        "matches": _count(con, "live_matches"),
        "leaderboards": _count(con, "live_leaderboards"),
        "last_run": latest,
        "stream_cache_seconds": get_settings().live_stream_cache_seconds,
        "schedule_cache_seconds": get_settings().live_schedule_cache_seconds,
    }


def site_live(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    tournaments = rows_to_dicts(
        con.execute(
            """
            SELECT tournament_slug, name, status, start_at, end_at, location_name, prize_pool, page_url, description, updated_at, raw_json
            FROM live_tournaments
            WHERE status IN ('live', 'upcoming')
            ORDER BY CASE status WHEN 'live' THEN 0 ELSE 1 END, start_at
            LIMIT 4
            """
        )
    ) if _table_exists(con, "live_tournaments") else []
    official_channel_names: set[str] = set()
    for tournament in tournaments:
        _repair_tournament_row(tournament)
        slug = tournament["tournament_slug"]
        tournament["watch_channels"] = rows_to_dicts(
            con.execute(
                """
                SELECT language, channel_name, channel_url, official
                FROM live_watch_channels
                WHERE tournament_slug = ?
                ORDER BY official DESC, language, channel_name
                """,
                [slug],
            )
        ) if _table_exists(con, "live_watch_channels") else []
        for channel in tournament["watch_channels"]:
            name = str(channel.get("channel_name") or "").strip().casefold()
            if name:
                official_channel_names.add(name)
        tournament["matches"] = rows_to_dicts(
            con.execute(
                """
                SELECT day_label, stage, match_label, best_of, scheduled_label, scheduled_at,
                       team_a, team_b, status, match_url, series_score_a, series_score_b, game_scores_json
                FROM live_matches
                WHERE tournament_slug = ?
                ORDER BY CASE status WHEN 'live' THEN 0 WHEN 'completed' THEN 1 ELSE 2 END, sort_order
                LIMIT 20
                """,
                [slug],
            )
        ) if _table_exists(con, "live_matches") else []
        for match in tournament["matches"]:
            match["games"] = json.loads(match.pop("game_scores_json") or "[]")
        tournament["matches"] = [
            match
            for match in tournament["matches"]
            if (match.get("team_a") and match.get("team_b")) or match.get("status") in {"live", "completed"}
        ][:12]
    tournaments.sort(
        key=lambda row: (
            0 if row.get("status") == "live" else 1,
            row.get("start_at") or datetime.max,
        )
    )

    streams = rows_to_dicts(
        con.execute(
            """
            SELECT title, author_name, platform, viewer_count, stream_url, embed_url, thumbnail_url,
                   started_at, is_live, classification, tournament_slug, channel_name
            FROM live_streams
            WHERE classification IN ('rlcs', 'pro')
            ORDER BY CASE classification WHEN 'rlcs' THEN 0 ELSE 1 END, viewer_count DESC, author_name
            LIMIT 12
            """
        )
    ) if _table_exists(con, "live_streams") else []
    for stream in streams:
        stream["is_official"] = str(stream.get("channel_name") or "").strip().casefold() in official_channel_names
    streams = [
        stream
        for stream in streams
        if stream.get("is_official") or stream.get("tournament_slug") or int(stream.get("viewer_count") or 0) >= 200
    ]
    streams.sort(
        key=lambda row: (
            bool(row.get("is_official")),
            bool(row.get("tournament_slug")),
            row.get("classification") == "rlcs",
            int(row.get("viewer_count") or 0),
        ),
        reverse=True,
    )
    streams = streams[:10]

    refreshed_at = _latest_updated_at(con, ["live_tournaments", "live_streams", "live_leaderboards"])
    featured = tournaments[0] if tournaments else None
    leaderboards = _leaderboards_payload(con)
    return {
        "refreshed_at": refreshed_at.isoformat() if refreshed_at else None,
        "auto_refresh_seconds": get_settings().live_stream_cache_seconds,
        "featured_tournament": featured,
        "tournaments": tournaments,
        "streams": streams,
        "leaderboards": leaderboards,
    }


def _parse_blast_tournament_index(html: str, now: datetime) -> list[dict[str, Any]]:
    objects = _json_ld_objects(html)
    current = now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now
    item_list = next(
        (
            item
            for item in objects
            if item.get("@type") == "ItemList"
            and item.get("itemListElement")
            and "rocket league" in (item.get("name") or "").lower()
        ),
        None,
    )
    if item_list is None:
        raise LiveSyncError("BLAST tournament index did not expose the expected ItemList metadata.")
    tournaments: list[dict[str, Any]] = []
    for wrapper in item_list.get("itemListElement") or []:
        event = wrapper.get("item") or {}
        name = event.get("name")
        url = event.get("url")
        if event.get("@type") != "SportsEvent" or not name or not url or "/rl/tournaments/" not in url:
            continue
        start_at = _parse_dt(event.get("startDate"))
        end_at = _parse_dt(event.get("endDate"))
        slug = url.rstrip("/").split("/")[-1]
        status = "upcoming"
        if start_at and end_at and start_at <= current <= end_at:
            status = "live"
        elif end_at and end_at < current:
            status = "finished"
        tournaments.append(
            {
                "tournament_slug": slug,
                "name": name,
                "status": status,
                "start_at": start_at,
                "end_at": end_at,
                "location_name": ((event.get("location") or {}).get("name")) or "TBA",
                "prize_pool": ((event.get("offers") or {}).get("price")) or None,
                "page_url": url.rstrip("/"),
                "description": event.get("description"),
                "raw": event,
            }
        )
    return tournaments


def _parse_watch_channels(html: str, tournament_slug: str, fetched_at: datetime) -> list[dict[str, Any]]:
    tokens = _text_tokens(html)
    try:
        start = tokens.index("Where to watch") + 1
    except ValueError:
        return []
    channels: list[dict[str, Any]] = []
    index = start
    while index + 1 < len(tokens):
        token = tokens[index]
        if token in {"Additional Info", "Overview", "Results", "Stats", "FAQ"} or _is_day_label(token):
            break
        if token.upper() in LANGUAGE_CODES:
            language = token.upper()
            channel_name = tokens[index + 1]
            if channel_name not in {"?", "TBD"}:
                channels.append(
                    {
                        "tournament_slug": tournament_slug,
                        "language": language,
                        "channel_name": channel_name,
                        "channel_url": f"https://www.twitch.tv/{channel_name}",
                        "official": channel_name.lower() in {"rocketleague", "rlesports"},
                        "updated_at": fetched_at,
                    }
                )
            index += 2
            continue
        index += 1
    return channels


def _parse_tournament_page(html: str, now: datetime) -> dict[str, Any]:
    objects = _json_ld_objects(html)
    event = next((item for item in objects if item.get("@type") == "SportsEvent"), None)
    if event is None:
        return {}
    current = now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now
    start_at = _parse_dt(event.get("startDate"))
    end_at = _parse_dt(event.get("endDate"))
    status = "upcoming"
    if start_at and end_at and start_at <= current <= end_at:
        status = "live"
    elif end_at and end_at < current:
        status = "finished"
    return {
        "status": status,
        "start_at": start_at,
        "end_at": end_at,
        "location_name": ((event.get("location") or {}).get("name")) or "TBA",
        "prize_pool": ((event.get("offers") or {}).get("price")) or None,
        "description": event.get("description"),
        "raw": event,
    }


def _parse_blast_matches(
    html: str,
    tournament_slug: str,
    fetched_at: datetime,
    *,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    tokens = _text_tokens(html)
    links = _series_links(html, base_url or "")
    matches: list[dict[str, Any]] = []
    current_day: str | None = None
    order = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _is_day_label(token):
            current_day = token
            index += 1
            continue
        if token in {"Overview", "Results", "Stats", "Live", "Past", "FAQ"} and current_day:
            break
        if (
            current_day
            and index + 9 < len(tokens)
            and tokens[index + 1] == ":"
            and BEST_OF_PATTERN.match(tokens[index + 3] or "")
            and tokens[index + 4] == tokens[index + 2]
            and tokens[index + 5] == tokens[index]
            and TIME_PATTERN.match(tokens[index + 7] or "")
        ):
            stage = tokens[index]
            match_label = tokens[index + 2]
            best_of = tokens[index + 3].upper()
            team_a = None if tokens[index + 6] in {"?", "TBD"} else tokens[index + 6]
            team_b = None if tokens[index + 8] in {"?", "TBD"} else tokens[index + 8]
            time_label = tokens[index + 7]
            scheduled_at = _scheduled_dt(current_day, time_label, fetched_at)
            order += 1
            matches.append(
                {
                    "match_id": f"{tournament_slug}:{order}",
                    "tournament_slug": tournament_slug,
                    "day_label": current_day,
                    "stage": stage,
                    "match_label": match_label,
                    "best_of": best_of,
                    "scheduled_label": f"{current_day} {time_label}",
                    "scheduled_at": scheduled_at,
                    "team_a": team_a,
                    "team_b": team_b,
                    "status": "scheduled",
                    "match_url": _best_series_link(
                        links,
                        stage=stage,
                        match_label=match_label,
                        best_of=best_of,
                        team_a=team_a,
                        team_b=team_b,
                    ),
                    "series_score_a": None,
                    "series_score_b": None,
                    "games": [],
                    "sort_order": order,
                }
            )
            index += 10
            continue
        index += 1
    return matches


def _series_links(html: str, base_url: str) -> list[dict[str, str]]:
    parser = _AnchorParser()
    parser.feed(html)
    links: list[dict[str, str]] = []
    for link in parser.links:
        href = link["href"]
        if "/series/" not in href:
            continue
        absolute = href if href.startswith("http") else urljoin(base_url or "https://blast.tv/", href)
        links.append({"url": absolute, "text": link["text"]})
    return links


def _best_series_link(
    links: list[dict[str, str]],
    *,
    stage: str,
    match_label: str,
    best_of: str,
    team_a: str | None,
    team_b: str | None,
) -> str | None:
    best_score = 0.0
    best_url: str | None = None
    for link in links:
        lowered = link["text"].lower()
        score = 0.0
        if stage.lower() in lowered:
            score += 1.4
        if match_label.lower() in lowered:
            score += 1.8
        if best_of.lower() in lowered:
            score += 1.0
        if team_a and team_a.lower() in lowered:
            score += 2.0
        if team_b and team_b.lower() in lowered:
            score += 2.0
        if score > best_score:
            best_score = score
            best_url = link["url"]
    return best_url if best_score >= 3.0 else None


def _parse_series_match_page(html: str, match: dict[str, Any]) -> dict[str, Any]:
    tokens = _text_tokens(html)
    updates: dict[str, Any] = {
        "status": match.get("status") or "scheduled",
        "series_score_a": None,
        "series_score_b": None,
        "games": [],
    }

    team_a = match.get("team_a")
    team_b = match.get("team_b")
    best_of = (match.get("best_of") or "").upper()
    target_wins = _series_target(best_of)
    details = _match_header_tokens(tokens)
    if team_a and team_b:
        for index in range(len(details) - 3):
            if details[index] != team_a or details[index + 2] != team_b:
                continue
            score_a = _score_or_none(details[index + 1])
            score_b = _score_or_none(details[index + 3])
            if score_a is None or score_b is None:
                continue
            updates["series_score_a"] = score_a
            updates["series_score_b"] = score_b
            break

    games = _parse_series_games(details)
    if games:
        updates["games"] = games

    has_live_header = any(" - LIVE" in token.upper() for token in details[:8])
    if updates["series_score_a"] is not None and updates["series_score_b"] is not None:
        if target_wins and max(updates["series_score_a"], updates["series_score_b"]) >= target_wins:
            updates["status"] = "completed"
        else:
            updates["status"] = "live"
    elif has_live_header or any(game.get("completed") for game in games):
        updates["status"] = "live"
    return updates


def _parse_rocketleague_tv_streams(
    html: str,
    *,
    official_channels: set[str],
    known_players: set[str],
    known_teams: set[str],
    tournaments: list[dict[str, Any]],
    fetched_at: datetime,
) -> list[dict[str, Any]]:
    objects = _json_ld_objects(html)
    item_list = next((item for item in objects if item.get("@type") == "ItemList" and item.get("itemListElement")), None)
    if item_list is None:
        raise LiveSyncError("RocketLeague.tv did not expose the expected ItemList metadata.")
    streams: list[dict[str, Any]] = []
    for wrapper in item_list.get("itemListElement") or []:
        item = wrapper.get("item") or {}
        author = item.get("author") or {}
        channel_name = (author.get("name") or "").strip()
        if not channel_name:
            continue
        title = item.get("name") or ""
        description = item.get("description") or ""
        stream_url = item.get("url") or author.get("url") or ""
        classification = _classify_stream(
            title=title,
            description=description,
            channel_name=channel_name,
            official_channels=official_channels,
            known_players=known_players,
            known_teams=known_teams,
        )
        tournament_slug = _associate_tournament(title, tournaments)
        streams.append(
            {
                "channel_name": channel_name,
                "title": title,
                "author_name": channel_name,
                "platform": _platform_from_url(stream_url),
                "viewer_count": _viewer_count(description),
                "stream_url": stream_url,
                "embed_url": item.get("embedUrl"),
                "thumbnail_url": item.get("thumbnailUrl"),
                "started_at": _parse_dt(((item.get("publication") or {}).get("startDate")) or item.get("uploadDate")),
                "is_live": bool(((item.get("publication") or {}).get("isLiveBroadcast"))),
                "classification": classification,
                "tournament_slug": tournament_slug,
                "updated_at": fetched_at,
                "raw": item,
            }
        )
    return streams


def _leaderboard_context_from_tournaments(tournaments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for tournament in tournaments:
        slug = tournament.get("tournament_slug") or ""
        major = re.search(r"major-(\d)-(\d{4})$", slug)
        if major:
            return {"season": major.group(2), "stage_key": f"major-{major.group(1)}"}
    return None


def _fetch_leaderboards(context: dict[str, Any], fetched_at: datetime) -> list[dict[str, Any]]:
    season = context["season"]
    stage_key = context["stage_key"]
    boards: list[dict[str, Any]] = []
    for region in ("eu", "na", "mena", "sam", "oce", "apac", "ssa"):
        source_url = f"https://blast.tv/rl/leaderboard/{season}/{stage_key}/{region}"
        try:
            html = _fetch_html(source_url)
            board = _parse_leaderboard_page(
                html,
                board_key=f"{season}:{stage_key}:{region}",
                board_name=f"{stage_key.upper()} {region.upper()}",
                stage_key=stage_key,
                region=region.upper(),
                source_url=source_url,
                fetched_at=fetched_at,
            )
        except Exception:
            continue
        if board["items"]:
            boards.append(board)
    return boards


def _parse_leaderboard_page(
    html: str,
    *,
    board_key: str,
    board_name: str,
    stage_key: str,
    region: str,
    source_url: str,
    fetched_at: datetime,
) -> dict[str, Any]:
    tokens = _text_tokens(html)
    start = None
    for index in range(len(tokens) - 3):
        if tokens[index:index + 4] == ["Rank", "Team", "Players", "Points"]:
            start = index + 4
            break
        if tokens[index:index + 3] == ["Rank", "Team", "Points"]:
            start = index + 3
            break
        compact = tokens[index].strip()
        if compact == "Rank Team Players Points":
            start = index + 1
            break
        if compact == "Rank Team Points":
            start = index + 1
            break
    items: list[dict[str, Any]] = []
    if start is None:
        return {
            "board_key": board_key,
            "board_name": board_name,
            "stage_key": stage_key,
            "region": region,
            "source_url": source_url,
            "updated_at": fetched_at,
            "items": items,
        }
    index = start
    while index < len(tokens):
        rank, consumed = _leaderboard_rank(tokens, index)
        if rank is None or index + consumed + 1 >= len(tokens):
            index += 1
            continue
        team_name = tokens[index + consumed]
        players: list[str] = []
        index += consumed + 1
        while index < len(tokens):
            compact_points = POINTS_PATTERN.fullmatch(tokens[index] or "")
            if compact_points:
                items.append(
                    {
                        "rank": rank,
                        "team_name": team_name,
                        "points": int(compact_points.group(1)),
                        "players": players,
                    }
                )
                index += 1
                break
            if index + 1 < len(tokens) and _is_int_token(tokens[index]) and tokens[index + 1] == "pts":
                items.append(
                    {
                        "rank": rank,
                        "team_name": team_name,
                        "points": int(tokens[index]),
                        "players": players,
                    }
                )
                index += 2
                break
            next_rank, _ = _leaderboard_rank(tokens, index)
            if next_rank is not None:
                break
            if "Qualifying Cut" in tokens[index]:
                index += 1
                continue
            players.append(tokens[index])
            index += 1
        if len(items) >= 10:
            break
    return {
        "board_key": board_key,
        "board_name": board_name,
        "stage_key": stage_key,
        "region": region,
        "source_url": source_url,
        "updated_at": fetched_at,
        "items": items,
    }


def _leaderboards_payload(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    if not _table_exists(con, "live_leaderboards"):
        return []
    rows = rows_to_dicts(
        con.execute(
            """
            SELECT board_key, board_name, stage_key, region, rank, team_name, points, players_json, source_url
            FROM live_leaderboards
            ORDER BY region, rank
            """
        )
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        board = grouped.setdefault(
            row["board_key"],
            {
                "board_key": row["board_key"],
                "board_name": row["board_name"],
                "stage_key": row["stage_key"],
                "region": row["region"],
                "source_url": row["source_url"],
                "items": [],
            },
        )
        board["items"].append(
            {
                "rank": row["rank"],
                "team_name": row["team_name"],
                "points": row["points"],
                "players": json.loads(row["players_json"] or "[]"),
            }
        )
    return [grouped[key] for key in sorted(grouped, key=lambda item: grouped[item]["region"])]


def _classify_stream(
    *,
    title: str,
    description: str,
    channel_name: str,
    official_channels: set[str],
    known_players: set[str],
    known_teams: set[str],
) -> str:
    haystack = f"{title} {description} {channel_name}".lower()
    if channel_name.lower() in official_channels or any(keyword in haystack for keyword in RLCS_KEYWORDS):
        return "rlcs"
    if any(name in haystack for name in known_players) or any(name in haystack for name in known_teams):
        return "pro"
    return "community"


def _split_game_token(token: str) -> tuple[str, str | None]:
    pieces = token.split(" ", 2)
    if len(pieces) < 2:
        return token, None
    label = " ".join(pieces[:2])
    map_name = pieces[2] if len(pieces) > 2 else None
    return label, map_name


def _series_target(best_of: str | None) -> int | None:
    if not best_of:
        return None
    match = re.fullmatch(r"BO(\d+)", best_of.upper())
    if not match:
        return None
    value = int(match.group(1))
    return (value // 2) + 1


def _match_header_tokens(tokens: list[str]) -> list[str]:
    start = 0
    for index, token in enumerate(tokens):
        if token.startswith("0000-00-00 - 00:00:") or re.fullmatch(r"\d{4}-\d{2}-\d{2} - \d{2}:\d{2}:", token):
            start = index
            break
    end = len(tokens)
    for marker in ("Predict The Winner", "### Stat Overview", "Stat Overview", "Stats", "Matchups", "FAQ"):
        try:
            end = min(end, tokens.index(marker, start))
        except ValueError:
            continue
    return tokens[start:end]


def _parse_series_games(tokens: list[str]) -> list[dict[str, Any]]:
    games: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        game_no: int | None = None
        map_parts: list[str] = []
        cursor = index
        token = tokens[index]
        if token == "Game" and index + 2 < len(tokens) and _is_int_token(tokens[index + 1]):
            game_no = int(tokens[index + 1])
            cursor = index + 2
            while cursor < len(tokens) and not _looks_like_series_score_token(tokens[cursor]) and not _is_game_marker(tokens[cursor]):
                map_parts.append(tokens[cursor])
                cursor += 1
        elif token.startswith("Game "):
            label, map_name = _split_game_token(token)
            match = re.fullmatch(r"Game (\d+)", label)
            if match:
                game_no = int(match.group(1))
                if map_name:
                    map_parts.append(map_name)
                cursor = index + 1
        else:
            index += 1
            continue
        if game_no is None:
            index += 1
            continue
        score_a = _score_or_none(tokens[cursor]) if cursor < len(tokens) else None
        cursor += 1 if cursor < len(tokens) else 0
        score_b = None
        while cursor < len(tokens) and not _is_game_marker(tokens[cursor]):
            score_b = _score_or_none(tokens[cursor])
            if score_b is not None or tokens[cursor] == "-":
                break
            cursor += 1
        games.append(
            {
                "label": f"Game {game_no}",
                "map_name": " ".join(map_parts).strip() or None,
                "score_a": score_a,
                "score_b": score_b,
                "completed": score_a is not None and score_b is not None,
            }
        )
        index = max(cursor, index + 1)
    return games


def _looks_like_series_score_token(token: str) -> bool:
    return token == "-" or _is_int_token(token)


def _is_game_marker(token: str) -> bool:
    return token == "Game" or token.startswith("Game ")


def _is_int_token(value: str) -> bool:
    return bool(re.fullmatch(r"\d+", value or ""))


def _leaderboard_rank(tokens: list[str], index: int) -> tuple[int | None, int]:
    token = tokens[index]
    if token == "#" and index + 1 < len(tokens) and _is_int_token(tokens[index + 1]):
        return int(tokens[index + 1]), 2
    compact = re.fullmatch(r"#(\d+)", token)
    if compact:
        return int(compact.group(1)), 1
    return None, 0


def _score_or_none(value: str) -> int | None:
    return int(value) if _is_int_token(value) else None


def _associate_tournament(title: str, tournaments: list[dict[str, Any]]) -> str | None:
    lowered = title.lower()
    for tournament in tournaments:
        name = (tournament.get("name") or "").lower()
        if not name:
            continue
        fragments = [fragment for fragment in re.split(r"[^a-z0-9]+", name) if len(fragment) > 2]
        if fragments and sum(1 for fragment in fragments if fragment in lowered) >= min(2, len(fragments)):
            return tournament.get("tournament_slug")
    return None


def _known_names(con: duckdb.DuckDBPyConnection, table_name: str, column_name: str) -> set[str]:
    if not _table_exists(con, table_name):
        return set()
    rows = con.execute(
        f"""
        SELECT DISTINCT lower({column_name})
        FROM {table_name}
        WHERE {column_name} IS NOT NULL
        LIMIT 500
        """
    ).fetchall()
    return {row[0] for row in rows if row[0] and len(row[0]) > 2}


def _known_team_names(con: duckdb.DuckDBPyConnection) -> set[str]:
    values: set[str] = set()
    if _table_exists(con, "remote_replays"):
        rows = con.execute(
            """
            SELECT DISTINCT lower(blue_team_name) FROM remote_replays WHERE blue_team_name IS NOT NULL
            UNION
            SELECT DISTINCT lower(orange_team_name) FROM remote_replays WHERE orange_team_name IS NOT NULL
            LIMIT 500
            """
        ).fetchall()
        values.update(row[0] for row in rows if row[0] and len(row[0]) > 2)
    return values


def _scheduled_dt(day_label: str, time_label: str, fetched_at: datetime) -> datetime | None:
    try:
        hour, minute = [int(part) for part in time_label.split(":", 1)]
    except ValueError:
        return None
    if day_label == "Today":
        day_value = fetched_at.date()
    elif day_label == "Tomorrow":
        day_value = fetched_at.date() + timedelta(days=1)
    elif DAY_PATTERN.match(day_label):
        try:
            parsed = datetime.strptime(f"{day_label} {fetched_at.year}", "%A, %B %d %Y")
            day_value = parsed.date()
        except ValueError:
            return None
    else:
        return None
    return datetime(day_value.year, day_value.month, day_value.day, hour, minute, tzinfo=timezone.utc)


def _fetch_html(url: str) -> str:
    request = Request(url, headers={"User-Agent": "ReplayOS/0.3 (+live sync)"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _json_ld_objects(html: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, flags=re.S | re.I):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
        elif isinstance(parsed, list):
            objects.extend(item for item in parsed if isinstance(item, dict))
    return objects


def _text_tokens(html: str) -> list[str]:
    parser = _TokenParser()
    parser.feed(html)
    return parser.tokens


def _viewer_count(description: str) -> int:
    match = VIEWER_PATTERN.search(description or "")
    if not match:
        return 0
    return int(match.group(1).replace(",", ""))


def _repair_tournament_row(tournament: dict[str, Any]) -> None:
    raw = tournament.pop("raw_json", None)
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return
    start_at = _coerce_dt_value(tournament.get("start_at")) or _parse_dt(payload.get("startDate"))
    end_at = _coerce_dt_value(tournament.get("end_at")) or _parse_dt(payload.get("endDate"))
    current = datetime.now(timezone.utc).replace(tzinfo=None)
    status = tournament.get("status") or "upcoming"
    if start_at and end_at and start_at <= current <= end_at:
        status = "live"
    elif end_at and end_at < current:
        status = "finished"
    tournament["start_at"] = start_at
    tournament["end_at"] = end_at
    tournament["status"] = status
    tournament["description"] = tournament.get("description") or payload.get("description")


def _platform_from_url(url: str) -> str:
    lowered = (url or "").lower()
    if "kick.com" in lowered:
        return "kick"
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "youtube"
    return "twitch"


def _coerce_dt_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return _parse_dt(value)
    return None


def _is_day_label(value: str) -> bool:
    return value in {"Today", "Tomorrow"} or bool(DAY_PATTERN.match(value or ""))


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _stale_snapshot(con: duckdb.DuckDBPyConnection, table_name: str, max_age_seconds: int) -> bool:
    if not _table_exists(con, table_name):
        return True
    row = con.execute(f"SELECT MAX(updated_at) FROM {table_name}").fetchone()
    if row is None or row[0] is None:
        return True
    return (datetime.now(timezone.utc) - row[0].replace(tzinfo=timezone.utc)).total_seconds() >= max_age_seconds


def _latest_updated_at(con: duckdb.DuckDBPyConnection, table_names: list[str]) -> datetime | None:
    latest: datetime | None = None
    for table_name in table_names:
        if not _table_exists(con, table_name):
            continue
        row = con.execute(f"SELECT MAX(updated_at) FROM {table_name}").fetchone()
        if row and row[0] is not None:
            value = row[0].replace(tzinfo=timezone.utc)
            if latest is None or value > latest:
                latest = value
    return latest


def _count(con: duckdb.DuckDBPyConnection, table_name: str) -> int:
    if not _table_exists(con, table_name):
        return 0
    return int(con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


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


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
