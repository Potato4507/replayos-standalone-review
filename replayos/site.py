from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any

import duckdb

from .ballchasing import ensure_ballchasing_replay_download
from .carball_ingest import ensure_replay_analysis, parsed_events, parsed_status_map, replay_name_coverage
from .config import get_settings
from .db import database_connection, rows_to_dicts
from .identity import IdentityResolver, alias_key, clean_identity_text, is_placeholder_team_name
from .semantics import build_replay_timeline
from .youtube_sync import replay_videos as synced_replay_videos

TEAM_ELO_CACHE_VERSION = "team-power-model-v6"
REPLAY_REVIEW_CACHE_VERSION = "replay-review-v2"
_RANKING_NOISE_PATTERN = re.compile(r"^(team ?\d+|\d+%?|\d+ ?x ?\d+)$", re.IGNORECASE)


def table_exists(con: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = con.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'main' AND table_name = ?
        """,
        [table_name],
    ).fetchone()
    return row is not None


def _fetch(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    return rows_to_dicts(con.execute(sql, params or []))


def _safe_fetch(con: duckdb.DuckDBPyConnection, table_name: str, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    if not table_exists(con, table_name):
        return []
    return _fetch(con, sql, params)


def _ensure_columns(con: duckdb.DuckDBPyConnection, table_name: str, column_defs: dict[str, str]) -> None:
    existing = _table_columns(con, table_name)
    for column_name, column_type in column_defs.items():
        if column_name not in existing:
            con.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _column_expr(columns: set[str], column_name: str, *, fallback_sql: str = "NULL") -> str:
    if column_name in columns:
        return column_name
    return f"{fallback_sql} AS {column_name}"


def _reset_team_elo_cache(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("DROP TABLE IF EXISTS team_elo_cache")
    ensure_site_cache_schema(con)


def _parse_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def ballchasing_status(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    configured = table_exists(con, "remote_sync_runs")
    last_run = None
    if configured:
        rows = _fetch(
            con,
            """
            SELECT run_id, started_at, completed_at, status, error, result_json
            FROM remote_sync_runs
            ORDER BY started_at DESC
            LIMIT 1
            """,
        )
        last_run = rows[0] if rows else None
        if last_run and last_run.get("result_json"):
            last_run["result"] = _parse_json(last_run.pop("result_json"), {})
    return {
        "configured_tables": configured,
        "remote_replays": _count(con, "remote_replays"),
        "remote_groups": _count(con, "remote_groups"),
        "remote_players": _count(con, "remote_players"),
        "last_run": last_run,
    }


def _count(con: duckdb.DuckDBPyConnection, table_name: str) -> int:
    if not table_exists(con, table_name):
        return 0
    return int(con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


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


def ensure_site_cache_schema(con: duckdb.DuckDBPyConnection) -> None:
    required_team_elo_columns = {
        "team_name",
        "rating",
        "elo",
        "power_score",
        "standings_score",
        "schedule_score",
        "form_score",
        "dominance_score",
        "quality_score",
        "tier_score",
        "standings_points",
        "standings_rank",
        "standings_region",
        "wins",
        "losses",
        "games",
        "win_rate",
        "confidence",
        "source_count",
        "last_delta",
        "avg_goal_diff",
        "strength_of_schedule",
        "recent_form",
        "last_match_date",
        "last_replay_id",
        "source_key",
    }
    if table_exists(con, "site_cache_meta") and table_exists(con, "team_elo_cache"):
        existing_columns = _table_columns(con, "team_elo_cache")
        if required_team_elo_columns.issubset(existing_columns):
            return
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS site_cache_meta (
            cache_key VARCHAR PRIMARY KEY,
            source_key VARCHAR,
            refreshed_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS team_elo_cache (
            team_name VARCHAR PRIMARY KEY,
            rating DOUBLE,
            elo DOUBLE,
            power_score DOUBLE,
            standings_score DOUBLE,
            schedule_score DOUBLE,
            form_score DOUBLE,
            dominance_score DOUBLE,
            quality_score DOUBLE,
            tier_score DOUBLE,
            standings_points BIGINT,
            standings_rank BIGINT,
            standings_region VARCHAR,
            wins BIGINT,
            losses BIGINT,
            games BIGINT,
            win_rate DOUBLE,
            confidence DOUBLE,
            source_count BIGINT,
            last_delta DOUBLE,
            avg_goal_diff DOUBLE,
            strength_of_schedule DOUBLE,
            recent_form DOUBLE,
            last_match_date TIMESTAMP,
            last_replay_id VARCHAR,
            source_key VARCHAR
        )
        """
    )
    _ensure_columns(
        con,
        "team_elo_cache",
        {
            "elo": "DOUBLE",
            "rating": "DOUBLE",
            "power_score": "DOUBLE",
            "standings_score": "DOUBLE",
            "schedule_score": "DOUBLE",
            "form_score": "DOUBLE",
            "dominance_score": "DOUBLE",
            "quality_score": "DOUBLE",
            "tier_score": "DOUBLE",
            "standings_points": "BIGINT",
            "standings_rank": "BIGINT",
            "standings_region": "VARCHAR",
            "win_rate": "DOUBLE",
            "confidence": "DOUBLE",
            "source_count": "BIGINT",
            "avg_goal_diff": "DOUBLE",
            "strength_of_schedule": "DOUBLE",
            "recent_form": "DOUBLE",
            "last_match_date": "TIMESTAMP",
            "last_replay_id": "VARCHAR",
            "source_key": "VARCHAR",
        },
    )


def ensure_replay_review_cache_schema(con: duckdb.DuckDBPyConnection) -> None:
    required_review_columns = {
        "replay_id",
        "source_key",
        "computed_at",
        "duration",
        "event_count",
        "base_blue_probability",
        "final_blue_probability",
        "volatility",
        "swing_count",
        "largest_blunder_player_name",
        "largest_blunder_label",
        "largest_blunder_impact",
        "largest_blunder_t",
        "best_play_player_name",
        "best_play_label",
        "best_play_impact",
        "best_play_t",
        "clutch_play_player_name",
        "clutch_play_label",
        "clutch_play_impact",
        "clutch_play_t",
        "turning_point_label",
        "turning_point_t",
        "turning_point_event_type",
        "blue_net",
        "orange_net",
        "eval_json",
        "win_edge_json",
        "player_impact_json",
        "turning_points_json",
    }
    if table_exists(con, "replay_review_cache"):
        existing_columns = _table_columns(con, "replay_review_cache")
        if required_review_columns.issubset(existing_columns):
            return
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS replay_review_cache (
            replay_id VARCHAR PRIMARY KEY,
            source_key VARCHAR,
            computed_at TIMESTAMP,
            duration DOUBLE,
            event_count BIGINT,
            base_blue_probability DOUBLE,
            final_blue_probability DOUBLE,
            volatility DOUBLE,
            swing_count BIGINT,
            largest_blunder_player_name VARCHAR,
            largest_blunder_label VARCHAR,
            largest_blunder_impact DOUBLE,
            largest_blunder_t DOUBLE,
            best_play_player_name VARCHAR,
            best_play_label VARCHAR,
            best_play_impact DOUBLE,
            best_play_t DOUBLE,
            clutch_play_player_name VARCHAR,
            clutch_play_label VARCHAR,
            clutch_play_impact DOUBLE,
            clutch_play_t DOUBLE,
            turning_point_label VARCHAR,
            turning_point_t DOUBLE,
            turning_point_event_type VARCHAR,
            blue_net DOUBLE,
            orange_net DOUBLE,
            eval_json VARCHAR,
            win_edge_json VARCHAR,
            player_impact_json VARCHAR,
            turning_points_json VARCHAR
        )
        """
    )
    _ensure_columns(
        con,
        "replay_review_cache",
        {
            "source_key": "VARCHAR",
            "computed_at": "TIMESTAMP",
            "duration": "DOUBLE",
            "event_count": "BIGINT",
            "base_blue_probability": "DOUBLE",
            "final_blue_probability": "DOUBLE",
            "volatility": "DOUBLE",
            "swing_count": "BIGINT",
            "largest_blunder_player_name": "VARCHAR",
            "largest_blunder_label": "VARCHAR",
            "largest_blunder_impact": "DOUBLE",
            "largest_blunder_t": "DOUBLE",
            "best_play_player_name": "VARCHAR",
            "best_play_label": "VARCHAR",
            "best_play_impact": "DOUBLE",
            "best_play_t": "DOUBLE",
            "clutch_play_player_name": "VARCHAR",
            "clutch_play_label": "VARCHAR",
            "clutch_play_impact": "DOUBLE",
            "clutch_play_t": "DOUBLE",
            "turning_point_label": "VARCHAR",
            "turning_point_t": "DOUBLE",
            "turning_point_event_type": "VARCHAR",
            "blue_net": "DOUBLE",
            "orange_net": "DOUBLE",
            "eval_json": "VARCHAR",
            "win_edge_json": "VARCHAR",
            "player_impact_json": "VARCHAR",
            "turning_points_json": "VARCHAR",
        },
    )


_SERIES_SIGNAL_TOKENS = {
    "vs", "playoff", "playoffs", "final", "finals", "semi", "quarter", "major", "regional",
    "world", "rlcs", "ewc", "gamers8", "fifae", "swiss", "group", "groups", "day", "open",
    "closed", "qualifier", "lower", "upper", "bracket", "grand", "series",
}
_SERIES_ROUND_TOKENS = {
    "playoff", "playoffs", "final", "finals", "semi", "quarter", "major", "regional",
    "world", "rlcs", "ewc", "gamers8", "fifae", "swiss", "group", "groups", "day",
    "open", "closed", "qualifier", "lower", "upper", "bracket", "grand", "decider",
}


def _series_name_quality(name: str | None) -> float:
    cleaned = clean_identity_text(name)
    if not cleaned:
        return 0.0
    key = alias_key(cleaned)
    if not key:
        return 0.0
    tokens = key.split()
    compact = "".join(tokens)
    score = 0.0
    if len(tokens) >= 2:
        score += 0.8
    if any(token in _SERIES_SIGNAL_TOKENS for token in tokens):
        score += 2.8
    if "vs" in tokens:
        score += 1.2
    if any(char.isdigit() for char in cleaned):
        score += 0.2
    if cleaned.startswith("[") and "grand finals" in key:
        score += 1.2
    if len(tokens) == 1 and re.fullmatch(r"[a-z]{7,}", compact) and compact not in _SERIES_SIGNAL_TOKENS:
        score -= 4.5
    if compact in {"nghjghkgm", "fdgdfgdfgdf"}:
        score -= 6.0
    return score


def _display_series_name(
    series_name: str | None,
    *,
    title: str | None = None,
    blue_team_name: str | None = None,
    orange_team_name: str | None = None,
) -> str | None:
    cleaned = clean_identity_text(series_name)
    if cleaned and _series_name_quality(cleaned) >= 1.5:
        return cleaned
    if blue_team_name and orange_team_name:
        return f"{blue_team_name} vs {orange_team_name}"
    if title and " vs " in title:
        return clean_identity_text(title)
    return cleaned if cleaned and len(cleaned) <= 4 and _series_name_quality(cleaned) >= 0.8 else None


def _series_matchup_identity(
    blue_team_name: str | None,
    orange_team_name: str | None,
) -> tuple[str | None, str | None]:
    blue = clean_identity_text(blue_team_name)
    orange = clean_identity_text(orange_team_name)
    if not blue or not orange:
        return (None, None)
    left = alias_key(blue)
    right = alias_key(orange)
    if not left or not right:
        return (None, None)
    ordered = sorted(((left, blue), (right, orange)), key=lambda item: item[0])
    return (f"{ordered[0][0]}::{ordered[1][0]}", f"{ordered[0][1]} vs {ordered[1][1]}")


def _series_kind(display_name: str, matchup_count: int) -> str:
    tokens = set(alias_key(display_name).split())
    if matchup_count > 1:
        return "round"
    if any(token in _SERIES_ROUND_TOKENS for token in tokens) and "vs" not in tokens:
        return "round"
    return "series"


def _series_candidate_score(
    display_name: str,
    *,
    replay_count: int,
    matchup_count: int,
    kind: str,
) -> float:
    score = float(replay_count) * 2.0
    score += min(max(matchup_count, 1), 4) * 0.8
    score += _series_name_quality(display_name)
    if kind == "series":
        score += 0.5
    if replay_count <= 1:
        score -= 4.0
    return score


def list_series(con: duckdb.DuckDBPyConnection, *, limit: int = 12) -> list[dict[str, Any]]:
    rows = _safe_fetch(
        con,
        "remote_replay_groups",
        """
        SELECT
            g.group_id,
            g.name,
            g.created_at,
            g.status,
            g.direct_replays,
            g.indirect_replays,
            rr.replay_id,
            rr.match_date,
            rr.blue_team_name,
            rr.orange_team_name
        FROM remote_groups g
        LEFT JOIN remote_replay_groups rg USING (group_id)
        LEFT JOIN remote_replays rr USING (replay_id)
        ORDER BY COALESCE(rr.match_date, g.created_at) DESC NULLS LAST, g.group_id, rr.match_date DESC NULLS LAST, rr.replay_id
        """,
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        group_id = row.get("group_id")
        if not group_id:
            continue
        bucket = grouped.setdefault(
            group_id,
            {
                "group_id": group_id,
                "name": row.get("name"),
                "created_at": row.get("created_at"),
                "status": row.get("status"),
                "direct_replays": row.get("direct_replays"),
                "indirect_replays": row.get("indirect_replays"),
                "replay_ids": set(),
                "matchups": {},
                "first_match_date": None,
                "last_match_date": None,
            },
        )
        replay_id = row.get("replay_id")
        if replay_id:
            bucket["replay_ids"].add(replay_id)
        match_dt = row.get("match_date")
        if match_dt:
            if not bucket["first_match_date"] or _coerce_match_dt(match_dt) < _coerce_match_dt(bucket["first_match_date"]):
                bucket["first_match_date"] = match_dt
            if not bucket["last_match_date"] or _coerce_match_dt(match_dt) > _coerce_match_dt(bucket["last_match_date"]):
                bucket["last_match_date"] = match_dt
        matchup_key, matchup_label = _series_matchup_identity(row.get("blue_team_name"), row.get("orange_team_name"))
        if matchup_key and matchup_label:
            bucket["matchups"][matchup_key] = matchup_label

    curated: dict[str, dict[str, Any]] = {}
    for bucket in grouped.values():
        replay_count = len(bucket["replay_ids"])
        display_name = _display_series_name(bucket.get("name"))
        if replay_count <= 0 or not display_name:
            continue
        matchup_labels = list(bucket["matchups"].values())
        matchup_count = len(matchup_labels)
        kind = _series_kind(display_name, matchup_count)
        if replay_count < 2 and matchup_count <= 1:
            continue
        item = {
            "group_id": bucket["group_id"],
            "name": display_name,
            "created_at": bucket.get("created_at"),
            "status": bucket.get("status"),
            "direct_replays": bucket.get("direct_replays"),
            "indirect_replays": bucket.get("indirect_replays"),
            "replay_count": replay_count,
            "first_match_date": bucket.get("first_match_date"),
            "last_match_date": bucket.get("last_match_date"),
            "matchup_count": matchup_count,
            "kind": kind,
            "kind_label": "Round" if kind == "round" else "Series",
            "matchup_name": matchup_labels[0] if matchup_count == 1 else None,
        }
        dedupe_key = (
            f"series:{alias_key(item['matchup_name'])}:{replay_count}"
            if kind == "series" and item.get("matchup_name")
            else f"round:{alias_key(display_name)}:{replay_count}"
        )
        item["_score"] = _series_candidate_score(display_name, replay_count=replay_count, matchup_count=matchup_count, kind=kind)
        current = curated.get(dedupe_key)
        if not current or item["_score"] > current["_score"]:
            curated[dedupe_key] = item

    ordered = sorted(
        curated.values(),
        key=lambda item: (
            _coerce_match_dt(item.get("last_match_date") or item.get("created_at")),
            item["_score"],
            item["replay_count"],
        ),
        reverse=True,
    )
    for item in ordered:
        item.pop("_score", None)
    return ordered[:limit]


def get_series(con: duckdb.DuckDBPyConnection, group_id: str) -> dict[str, Any] | None:
    groups = _safe_fetch(con, "remote_groups", "SELECT * FROM remote_groups WHERE group_id = ?", [group_id])
    if not groups:
        return None
    series = groups[0]
    series["players"] = _parse_json(series.pop("players_json", None), [])
    series["raw"] = _parse_json(series.pop("raw_json", None), {})
    series["matches"] = _safe_fetch(
        con,
        "remote_replay_groups",
        """
        SELECT
            rr.replay_id,
            rr.title,
            rr.match_date,
            rr.playlist_id,
            rr.duration,
            rr.blue_team_name,
            rr.blue_goals,
            rr.orange_team_name,
            rr.orange_goals,
            rr.local_file_path,
            r.has_semantic_features
        FROM remote_replay_groups rg
        JOIN remote_replays rr USING (replay_id)
        LEFT JOIN replays r USING (replay_id)
        WHERE rg.group_id = ?
        ORDER BY rr.match_date DESC NULLS LAST, rr.created_at DESC NULLS LAST
        """,
        [group_id],
    )
    _apply_identity_display(con, series["matches"])
    _apply_parse_status(con, series["matches"])
    _attach_replay_review_summaries(con, series["matches"])
    return series


def list_library_replays(
    con: duckdb.DuckDBPyConnection,
    *,
    limit: int = 24,
    offset: int = 0,
    group_id: str | None = None,
    search: str | None = None,
    parsed_only: bool = False,
    review_ready: bool = False,
    sort_mode: str = "recent",
) -> list[dict[str, Any]]:
    return library_replay_page(
        con,
        limit=limit,
        offset=offset,
        group_id=group_id,
        search=search,
        parsed_only=parsed_only,
        review_ready=review_ready,
        sort_mode=sort_mode,
    )["items"]


def library_replay_page(
    con: duckdb.DuckDBPyConnection,
    *,
    limit: int = 24,
    offset: int = 0,
    group_id: str | None = None,
    search: str | None = None,
    parsed_only: bool = False,
    review_ready: bool = False,
    sort_mode: str = "recent",
) -> dict[str, Any]:
    ensure_replay_review_cache_schema(con)
    offset = max(0, int(offset))
    limit = max(1, int(limit))
    sort_mode = _normalize_library_sort_mode(sort_mode)
    rows, total = _library_rows_and_total(
        con,
        limit=limit,
        offset=offset,
        group_id=group_id,
        search=search,
        parsed_only=parsed_only,
        review_ready=review_ready,
        sort_mode=sort_mode,
    )
    for row in rows:
        row["local_file_path"] = row.get("local_file_path") or _local_replay_path(row["replay_id"])
    _apply_identity_display(con, rows)
    for row in rows:
        row["series_name"] = _display_series_name(
            row.get("series_name"),
            title=row.get("title"),
            blue_team_name=row.get("blue_team_name"),
            orange_team_name=row.get("orange_team_name"),
        )
    _apply_parse_status(con, rows)
    _attach_replay_review_summaries(con, rows)
    return {
        "items": rows,
        "total": total,
        "offset": offset,
        "limit": limit,
        "sort_mode": sort_mode,
        "has_more": (offset + len(rows)) < total,
    }


def _library_rows_and_total(
    con: duckdb.DuckDBPyConnection,
    *,
    limit: int,
    offset: int,
    group_id: str | None,
    search: str | None,
    parsed_only: bool,
    review_ready: bool,
    sort_mode: str,
) -> tuple[list[dict[str, Any]], int]:
    has_parsed_status = table_exists(con, "replay_parsed_status")
    parsed_join = "LEFT JOIN replay_parsed_status ps USING (replay_id)" if has_parsed_status else ""
    has_remote_groups = table_exists(con, "remote_replay_groups")
    prediction_sql = (
        """
        (
            SELECT prediction_type
            FROM predictions p
            WHERE p.replay_id = rr.replay_id
            ORDER BY created_at DESC
            LIMIT 1
        ) AS latest_prediction_type
        """
        if table_exists(con, "predictions")
        else "NULL AS latest_prediction_type"
    )
    union_parts: list[str] = []
    union_params: list[Any] = []

    if table_exists(con, "remote_replays"):
        remote_columns = _table_columns(con, "remote_replays")
        has_replays_table = table_exists(con, "replays")
        has_matches_table = table_exists(con, "matches")
        remote_parsed_join = "LEFT JOIN replay_parsed_status rps USING (replay_id)" if has_parsed_status else ""
        remote_replays_join = "LEFT JOIN replays r USING (replay_id)" if has_replays_table else ""
        remote_matches_join = "LEFT JOIN matches m USING (replay_id)" if has_matches_table else ""
        remote_semantic_expr = "COALESCE(r.has_semantic_features, FALSE)" if has_replays_table else "FALSE"
        remote_winner_expr = "m.winner_team_id" if has_matches_table else "NULL"
        remote_raw_blue_goals = (
            "TRY_CAST(json_extract_string(rr.raw_json, '$.blue.stats.core.goals') AS BIGINT)"
            if "raw_json" in remote_columns
            else "NULL"
        )
        remote_raw_orange_goals = (
            "TRY_CAST(json_extract_string(rr.raw_json, '$.orange.stats.core.goals') AS BIGINT)"
            if "raw_json" in remote_columns
            else "NULL"
        )
        remote_blue_goals_expr = (
            f"COALESCE(rps.blue_goals, rr.blue_goals, {remote_raw_blue_goals})"
            if has_parsed_status
            else f"COALESCE(rr.blue_goals, {remote_raw_blue_goals})"
        )
        remote_orange_goals_expr = (
            f"COALESCE(rps.orange_goals, rr.orange_goals, {remote_raw_orange_goals})"
            if has_parsed_status
            else f"COALESCE(rr.orange_goals, {remote_raw_orange_goals})"
        )
        remote_group_id_fallback = "json_extract_string(rr.group_ids_json, '$[0]')" if "group_ids_json" in remote_columns else "NULL"
        remote_group_name_fallback = "json_extract_string(rr.group_names_json, '$[0]')" if "group_names_json" in remote_columns else "NULL"
        remote_group_join = ""
        remote_series_id = remote_group_id_fallback
        remote_series_name = remote_group_name_fallback
        remote_series_index = "NULL"
        remote_series_count = "NULL"
        remote_series_anchor = "COALESCE(epoch(rr.match_date), epoch(rr.created_at))"
        if has_remote_groups:
            remote_group_join = """
            LEFT JOIN (
                SELECT replay_id, group_id, group_name
                FROM (
                    SELECT
                        replay_id,
                        group_id,
                        group_name,
                        ROW_NUMBER() OVER (PARTITION BY replay_id ORDER BY group_name, group_id) AS replay_group_rank
                    FROM remote_replay_groups
                ) ranked_remote_groups
                WHERE replay_group_rank = 1
            ) rg1 USING (replay_id)
            """
            remote_series_id = f"COALESCE(rg1.group_id, {remote_group_id_fallback})"
            remote_series_name = f"COALESCE(rg1.group_name, {remote_group_name_fallback})"
            remote_partition_id = f"COALESCE(rg1.group_id, {remote_group_id_fallback})"
            remote_series_index = (
                f"CASE WHEN {remote_partition_id} IS NOT NULL THEN "
                f"ROW_NUMBER() OVER (PARTITION BY {remote_partition_id} ORDER BY COALESCE(rr.match_date, rr.created_at), rr.replay_id) "
                "ELSE NULL END"
            )
            remote_series_count = (
                f"CASE WHEN {remote_partition_id} IS NOT NULL THEN "
                f"COUNT(*) OVER (PARTITION BY {remote_partition_id}) "
                "ELSE NULL END"
            )
            remote_series_anchor = (
                f"CASE WHEN {remote_partition_id} IS NOT NULL THEN "
                f"MAX(COALESCE(epoch(rr.match_date), epoch(rr.created_at))) OVER (PARTITION BY {remote_partition_id}) "
                "ELSE COALESCE(epoch(rr.match_date), epoch(rr.created_at)) END"
            )
        remote_parse_priority = (
            "CASE WHEN EXISTS (SELECT 1 FROM replay_parsed_status ps3 WHERE ps3.replay_id = rr.replay_id AND ps3.status = 'completed') THEN 1 ELSE 0 END"
            if has_parsed_status
            else "0"
        )
        filters: list[str] = []
        params: list[Any] = []
        if group_id:
            filters.append("EXISTS (SELECT 1 FROM remote_replay_groups rg WHERE rg.replay_id = rr.replay_id AND rg.group_id = ?)")
            params.append(group_id)
        if search:
            filters.append("(rr.title ILIKE ? OR rr.blue_team_name ILIKE ? OR rr.orange_team_name ILIKE ? OR rr.replay_id ILIKE ?)")
            params.extend([f"%{search}%"] * 4)
        if parsed_only:
            if has_parsed_status:
                filters.append(
                    "("
                    "EXISTS (SELECT 1 FROM replay_parsed_status ps2 WHERE ps2.replay_id = rr.replay_id AND ps2.status = 'completed') "
                    f"OR {remote_semantic_expr}"
                    ")"
                )
            else:
                filters.append(remote_semantic_expr)
        if review_ready:
            filters.append("EXISTS (SELECT 1 FROM replay_review_cache rrc WHERE rrc.replay_id = rr.replay_id)")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        union_parts.append(
            f"""
            SELECT
                rr.replay_id,
                rr.title,
                COALESCE(epoch(rr.match_date), epoch(rr.created_at)) AS match_date,
                rr.duration,
                rr.playlist_id,
                rr.blue_team_name,
                {remote_blue_goals_expr} AS blue_goals,
                rr.orange_team_name,
                {remote_orange_goals_expr} AS orange_goals,
                rr.local_file_path,
                epoch(rr.downloaded_at) AS downloaded_at,
                {remote_semantic_expr} AS has_semantic_features,
                {remote_winner_expr} AS winner_team_id,
                {prediction_sql},
                COALESCE(epoch(rr.match_date), epoch(rr.created_at)) AS sort_ts,
                {remote_series_id} AS series_group_id,
                {remote_series_name} AS series_name,
                {remote_series_index} AS series_replay_index,
                {remote_series_count} AS series_replay_count,
                {remote_series_anchor} AS series_anchor_ts,
                2 AS source_priority,
                {remote_parse_priority} AS parse_priority
            FROM remote_replays rr
            {remote_parsed_join}
            {remote_replays_join}
            {remote_matches_join}
            {remote_group_join}
            {where}
            """
        )
        union_params.extend(params)

    if table_exists(con, "replays") and not group_id:
        filters = []
        params = []
        if search:
            if parsed_join:
                filters.append(
                    "("
                    "r.replay_id ILIKE ? "
                    "OR COALESCE(ps.blue_team_name, '') ILIKE ? "
                    "OR COALESCE(ps.orange_team_name, '') ILIKE ?"
                    ")"
                )
                params.extend([f"%{search}%"] * 3)
            else:
                filters.append("r.replay_id ILIKE ?")
                params.append(f"%{search}%")
        if parsed_only:
            if parsed_join:
                filters.append("(ps.status = 'completed' OR COALESCE(r.has_semantic_features, FALSE))")
            else:
                filters.append("COALESCE(r.has_semantic_features, FALSE)")
        if review_ready:
            filters.append("EXISTS (SELECT 1 FROM replay_review_cache rrc WHERE rrc.replay_id = r.replay_id)")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        parsed_duration = "COALESCE(ps.duration_seconds, r.game_duration)" if parsed_join else "r.game_duration"
        parsed_blue_name = "COALESCE(ps.blue_team_name, 'Blue Side')" if parsed_join else "'Blue Side'"
        parsed_blue_goals = "COALESCE(ps.blue_goals, m.blue_goals)" if parsed_join else "m.blue_goals"
        parsed_orange_name = "COALESCE(ps.orange_team_name, 'Orange Side')" if parsed_join else "'Orange Side'"
        parsed_orange_goals = "COALESCE(ps.orange_goals, m.orange_goals)" if parsed_join else "m.orange_goals"
        parse_priority = "CASE WHEN ps.status = 'completed' THEN 1 ELSE 0 END" if parsed_join else "0"
        local_ingested_at = "COALESCE(TRY_CAST(r.ingested_at AS DOUBLE), epoch(TRY_CAST(r.ingested_at AS TIMESTAMP)), 0.0)"
        union_parts.append(
            f"""
            SELECT
                r.replay_id,
                CASE
                    WHEN {parsed_blue_name} <> 'Blue Side' AND {parsed_orange_name} <> 'Orange Side'
                    THEN {parsed_blue_name} || ' vs ' || {parsed_orange_name}
                    ELSE r.replay_id
                END AS title,
                {local_ingested_at} AS match_date,
                {parsed_duration} AS duration,
                'local' AS playlist_id,
                {parsed_blue_name} AS blue_team_name,
                {parsed_blue_goals} AS blue_goals,
                {parsed_orange_name} AS orange_team_name,
                {parsed_orange_goals} AS orange_goals,
                NULL AS local_file_path,
                NULL AS downloaded_at,
                COALESCE(r.has_semantic_features, FALSE) AS has_semantic_features,
                m.winner_team_id,
                NULL AS latest_prediction_type,
                {local_ingested_at} AS sort_ts,
                NULL AS series_group_id,
                NULL AS series_name,
                NULL AS series_replay_index,
                NULL AS series_replay_count,
                {local_ingested_at} AS series_anchor_ts,
                1 AS source_priority,
                {parse_priority} AS parse_priority
            FROM replays r
            LEFT JOIN matches m USING (replay_id)
            {parsed_join}
            {where}
            """
        )
        union_params.extend(params)

    if not union_parts:
        return [], 0

    combined_sql = " UNION ALL ".join(union_parts)
    page_order = _library_order_clause(sort_mode)
    total_row = con.execute(
        f"""
        WITH combined AS (
            {combined_sql}
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY replay_id
                       ORDER BY source_priority DESC, parse_priority DESC, sort_ts DESC NULLS LAST, replay_id
                   ) AS dedupe_rank
            FROM combined
        )
        SELECT COUNT(*)
        FROM ranked
        WHERE dedupe_rank = 1
        """,
        union_params,
    ).fetchone()
    total = int(total_row[0] or 0) if total_row else 0
    if total <= 0:
        return [], 0

    rows = _fetch(
        con,
        f"""
        WITH combined AS (
            {combined_sql}
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY replay_id
                       ORDER BY source_priority DESC, parse_priority DESC, sort_ts DESC NULLS LAST, replay_id
                   ) AS dedupe_rank
            FROM combined
        )
        SELECT
            replay_id,
            title,
            match_date,
            duration,
            playlist_id,
            blue_team_name,
            blue_goals,
            orange_team_name,
            orange_goals,
            local_file_path,
            downloaded_at,
            has_semantic_features,
            winner_team_id,
            latest_prediction_type,
            series_group_id,
            series_name,
            series_replay_index,
            series_replay_count
        FROM ranked
        WHERE dedupe_rank = 1
        ORDER BY {page_order}
        LIMIT ? OFFSET ?
        """,
        [*union_params, limit, offset],
    )
    return rows, total


def _normalize_library_sort_mode(value: str | None) -> str:
    normalized = (value or "recent").strip().lower()
    if normalized in {"series", "series-order", "series_order"}:
        return "series"
    return "recent"


def _library_order_clause(sort_mode: str) -> str:
    if sort_mode == "series":
        return (
            "CASE WHEN series_group_id IS NULL THEN 1 ELSE 0 END, "
            "series_anchor_ts DESC NULLS LAST, "
            "LOWER(COALESCE(series_name, title)) ASC, "
            "COALESCE(series_replay_index, 999999) ASC, "
            "source_priority DESC, parse_priority DESC, sort_ts DESC NULLS LAST, replay_id"
        )
    return "source_priority DESC, parse_priority DESC, has_semantic_features DESC, sort_ts DESC NULLS LAST, replay_id"


def get_library_replay(con: duckdb.DuckDBPyConnection, replay_id: str) -> dict[str, Any] | None:
    has_replays_table = table_exists(con, "replays")
    has_matches_table = table_exists(con, "matches")
    remote_replays_join = "LEFT JOIN replays r USING (replay_id)" if has_replays_table else ""
    remote_matches_join = "LEFT JOIN matches m USING (replay_id)" if has_matches_table else ""
    remote_has_semantic_expr = "r.has_semantic_features" if has_replays_table else "FALSE AS has_semantic_features"
    remote_duration_expr = "r.game_duration" if has_replays_table else "NULL AS game_duration"
    remote_winner_expr = "m.winner_team_id" if has_matches_table else "NULL AS winner_team_id"
    rows = _safe_fetch(
        con,
        "remote_replays",
        f"""
        SELECT rr.*, {remote_has_semantic_expr}, {remote_duration_expr}, {remote_winner_expr}
        FROM remote_replays rr
        {remote_replays_join}
        {remote_matches_join}
        WHERE rr.replay_id = ?
        """,
        [replay_id],
    )
    if rows:
        replay = rows[0]
        replay["local_file_path"] = replay.get("local_file_path") or _local_replay_path(replay_id)
        replay["group_ids"] = _parse_json(replay.pop("group_ids_json", None), [])
        replay["group_names"] = _parse_json(replay.pop("group_names_json", None), [])
        replay["raw"] = _parse_json(replay.pop("raw_json", None), {})
        _fill_remote_score_from_raw(replay)
        replay["players"] = _safe_fetch(
            con,
            "remote_players",
            """
            SELECT side, platform, platform_player_id, player_name, car_name, score, goals, assists, saves, shots,
                   demos_inflicted, boost_bpm, avg_speed, percent_behind_ball
            FROM remote_players
            WHERE replay_id = ?
            ORDER BY side, score DESC NULLS LAST, player_name
            """,
            [replay_id],
        )
        replay["series"] = _safe_fetch(
            con,
            "remote_replay_groups",
            """
            SELECT group_id, group_name
            FROM remote_replay_groups
            WHERE replay_id = ?
            ORDER BY group_name
            """,
            [replay_id],
        )
        for series_row in replay["series"]:
            series_row["group_name"] = _display_series_name(
                series_row.get("group_name"),
                title=replay.get("title"),
                blue_team_name=replay.get("blue_team_name"),
                orange_team_name=replay.get("orange_team_name"),
            ) or series_row.get("group_name")
        replay["videos"] = synced_replay_videos(con, replay_id)
        if not replay["players"]:
            replay["players"] = _parsed_player_boxscore(con, replay_id)
        _apply_identity_display(con, [replay])
        replay["players"] = _canonicalize_boxscores(con, replay["players"])
        _apply_parse_status(con, [replay])
        review_payload, _ = _ensure_replay_review(con, replay_id, duration=replay.get("duration"))
        if review_payload:
            replay["review"] = review_payload["summary"]
        return replay
    if not table_exists(con, "replays"):
        return None
    parsed_join = "LEFT JOIN replay_parsed_status ps USING (replay_id)" if table_exists(con, "replay_parsed_status") else ""
    local = _fetch(
        con,
        f"""
        SELECT
            r.*,
            {"COALESCE(ps.blue_goals, m.blue_goals)" if parsed_join else "m.blue_goals"} AS blue_goals,
            {"COALESCE(ps.orange_goals, m.orange_goals)" if parsed_join else "m.orange_goals"} AS orange_goals,
            m.winner_team_id,
            {"ps.blue_team_name" if parsed_join else "NULL"} AS parsed_blue_team_name,
            {"ps.orange_team_name" if parsed_join else "NULL"} AS parsed_orange_team_name,
            {"ps.duration_seconds" if parsed_join else "NULL"} AS parsed_duration_seconds
        FROM replays r
        LEFT JOIN matches m USING (replay_id)
        {parsed_join}
        WHERE r.replay_id = ?
        """,
        [replay_id],
    )
    if not local:
        return None
    replay = local[0]
    blue_name = replay.pop("parsed_blue_team_name", None)
    orange_name = replay.pop("parsed_orange_team_name", None)
    parsed_duration = replay.pop("parsed_duration_seconds", None)
    replay.update(
        {
            "title": f"{blue_name} vs {orange_name}" if blue_name and orange_name else replay_id,
            "match_date": replay.get("ingested_at"),
            "blue_team_name": blue_name or "Blue Side",
            "orange_team_name": orange_name or "Orange Side",
            "duration": parsed_duration or replay.get("game_duration"),
            "players": _parsed_player_boxscore(con, replay_id),
            "series": [],
            "group_ids": [],
            "group_names": [],
            "raw": {},
            "local_file_path": _local_replay_path(replay_id),
            "videos": synced_replay_videos(con, replay_id),
        }
    )
    _apply_identity_display(con, [replay])
    replay["players"] = _canonicalize_boxscores(con, replay["players"])
    _apply_parse_status(con, [replay])
    review_payload, _ = _ensure_replay_review(con, replay_id, duration=replay.get("duration"))
    if review_payload:
        replay["review"] = review_payload["summary"]
    return replay


def site_home(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    coverage = replay_name_coverage(con)
    review_status = replay_review_status(con)
    summary_counts = {
        "local_replays": _count(con, "replays"),
        "remote_replays": _count(con, "remote_replays"),
        "series": _count(con, "remote_groups"),
        "players": _count(con, "players"),
        "youtube_videos": _count(con, "replay_videos"),
        "parsed_replays": _count_completed_parses(con),
        "indexed_local_replays": coverage["indexed_local_replays"],
        "named_team_replays": coverage["named_team_replays"],
        "named_player_replays": coverage["named_player_replays"],
        "name_coverage_rate": coverage["coverage_rate"],
        "orphan_local_replays": coverage["orphan_local_replays"],
        "eval_ready_replays": review_status["cached_replays"],
        "eval_pending_replays": review_status["missing_replays"],
    }
    event_source = "replay_parsed_events" if table_exists(con, "replay_parsed_events") else "events"
    event_counts = _safe_fetch(
        con,
        event_source,
        f"""
        SELECT event_type, COUNT(*) AS n
        FROM {event_source}
        WHERE event_type IN (
            'goal',
            'touch',
            'demo',
            'turnover',
            'pressure_phase',
            'kickoff_outcome',
            'boost_starvation_window',
            'overcommit_proxy'
        )
        GROUP BY event_type
        ORDER BY
            CASE event_type
                WHEN 'goal' THEN 0
                WHEN 'turnover' THEN 1
                WHEN 'pressure_phase' THEN 2
                WHEN 'demo' THEN 3
                WHEN 'kickoff_outcome' THEN 4
                WHEN 'boost_starvation_window' THEN 5
                WHEN 'overcommit_proxy' THEN 6
                ELSE 7
            END,
            n DESC
        LIMIT 8
        """,
    )
    return {
        "counts": summary_counts,
        "series": list_series(con, limit=6),
        "matches": list_library_replays(con, limit=10),
        "team_elo": team_elo_index(con, limit=10),
        "event_counts": event_counts,
        "top_players": top_remote_players(con, limit=10),
        "sync": ballchasing_status(con),
        "coverage": coverage,
        "review_status": review_status,
    }


def top_remote_players(con: duckdb.DuckDBPyConnection, *, limit: int = 12) -> list[dict[str, Any]]:
    rows = _safe_fetch(
        con,
        "remote_players",
        """
        SELECT
            player_name,
            platform,
            platform_player_id,
            COUNT(DISTINCT replay_id) AS replays,
            SUM(COALESCE(goals, 0)) AS goals,
            SUM(COALESCE(assists, 0)) AS assists,
            SUM(COALESCE(saves, 0)) AS saves,
            AVG(COALESCE(score, 0)) AS avg_score,
            AVG(COALESCE(boost_bpm, 0)) AS avg_boost_bpm
        FROM remote_players
        WHERE player_name IS NOT NULL
        GROUP BY 1, 2, 3
        ORDER BY goals DESC, assists DESC, avg_score DESC
        LIMIT ?
        """,
        [limit],
    )
    if rows:
        return _canonicalize_top_players(con, rows, limit=limit)
    parsed_rows = _safe_fetch(
        con,
        "replay_parsed_events",
        """
        SELECT
            COALESCE(player_name, player_id) AS player_name,
            NULL AS platform,
            player_id AS platform_player_id,
            COUNT(DISTINCT replay_id) AS replays,
            SUM(CASE WHEN event_type = 'goal' THEN 1 ELSE 0 END) AS goals,
            NULL AS assists,
            NULL AS saves,
            AVG(
                CASE
                    WHEN event_type = 'touch' THEN 4
                    WHEN event_type = 'goal' THEN 100
                    WHEN event_type = 'demo' THEN 15
                    WHEN event_type = 'pressure_phase' THEN 10
                    ELSE 0
                END
            ) AS avg_score,
            NULL AS avg_boost_bpm
        FROM replay_parsed_events
        WHERE player_name IS NOT NULL
        GROUP BY 1, 2, 3
        ORDER BY goals DESC, replays DESC, avg_score DESC
        LIMIT ?
        """,
        [limit],
    )
    if parsed_rows:
        return _canonicalize_top_players(con, parsed_rows, limit=limit)
    return _canonicalize_top_players(
        con,
        _fetch(
        con,
        """
        SELECT
            player_name,
            NULL AS platform,
            player_id AS platform_player_id,
            replay_count AS replays,
            goals,
            NULL AS assists,
            NULL AS saves,
            NULL AS avg_score,
            NULL AS avg_boost_bpm
        FROM players
        ORDER BY goals DESC, touches DESC, replay_count DESC
        LIMIT ?
        """,
        [limit],
        ),
        limit=limit,
    )


def team_elo_index(con: duckdb.DuckDBPyConnection, *, limit: int = 16) -> list[dict[str, Any]]:
    try:
        return _team_elo_index_impl(con, limit=limit)
    except duckdb.InvalidInputException as exc:
        if "read-only mode" not in str(exc).lower():
            raise
        return _read_team_elo_cache(con, limit=limit)
    except duckdb.ConnectionException:
        return _read_team_elo_cache(con, limit=limit)


def _is_noisy_ranking_name(team_name: str | None) -> bool:
    cleaned = clean_identity_text(team_name)
    if not cleaned:
        return True
    key = alias_key(cleaned)
    if not key or len(key) <= 1:
        return True
    if _RANKING_NOISE_PATTERN.fullmatch(key):
        return True
    if "%" in cleaned and sum(1 for char in cleaned if char.isalpha()) < 3:
        return True
    return False


def _team_elo_index_impl(con: duckdb.DuckDBPyConnection, *, limit: int = 16) -> list[dict[str, Any]]:
    if not any(table_exists(con, table_name) for table_name in ("remote_replays", "replay_parsed_status", "live_leaderboards")):
        return []
    ensure_site_cache_schema(con)
    source_key = _team_elo_source_key(con)
    cached_key = con.execute(
        "SELECT source_key FROM site_cache_meta WHERE cache_key = 'team_elo'"
    ).fetchone()
    if not cached_key or cached_key[0] != source_key or _count(con, "team_elo_cache") == 0:
        _refresh_team_elo_cache(con, source_key)
    items = _read_team_elo_cache(con, limit=limit)
    if not items and _count(con, "team_elo_cache") > 0:
        _refresh_team_elo_cache(con, source_key)
        items = _read_team_elo_cache(con, limit=limit)
    return items


def _read_team_elo_cache(con: duckdb.DuckDBPyConnection, *, limit: int) -> list[dict[str, Any]]:
    if not table_exists(con, "team_elo_cache"):
        return []
    rows = _fetch(
        con,
        """
        SELECT
            team_name,
            rating,
            elo,
            power_score,
            standings_score,
            schedule_score,
            form_score,
            dominance_score,
            quality_score,
            tier_score,
            standings_points,
            standings_rank,
            standings_region,
            wins,
            losses,
            games,
            win_rate,
            confidence,
            source_count,
            last_delta,
            avg_goal_diff,
            strength_of_schedule,
            recent_form,
            last_match_date,
            last_replay_id
        FROM team_elo_cache
        ORDER BY rating DESC NULLS LAST, standings_score DESC NULLS LAST, standings_points DESC NULLS LAST, games DESC, elo DESC, team_name
        LIMIT ?
        """,
        [max(limit * 3, limit)],
    )
    if not rows:
        return []
    curated: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    for row in rows:
        games = int(row.get("games") or 0)
        standings_points = int(row.get("standings_points") or 0)
        confidence = float(row.get("confidence") or 0.0)
        if _is_noisy_ranking_name(row.get("team_name")):
            continue
        if confidence >= 0.33 and (games >= 5 or standings_points > 0):
            curated.append(row)
        elif standings_points > 0 or (games >= 6 and confidence >= 0.36):
            fallback.append(row)
    usable = (curated + fallback)[:limit]
    if usable:
        return usable
    return [row for row in rows if not _is_noisy_ranking_name(row.get("team_name"))][:limit]


def _refresh_team_elo_cache(con: duckdb.DuckDBPyConnection, source_key: str) -> None:
    resolver = IdentityResolver(con)
    rows = _power_match_rows(con)
    teams: dict[str, dict[str, Any]] = {}
    latest_match_dt = max((_coerce_match_dt(row.get("match_date")) for row in rows), default=datetime.now(timezone.utc))
    for row in rows:
        replay_id = row["replay_id"]
        match_date = _coerce_match_dt(row.get("match_date"))
        blue_name = row["blue_team_name"]
        orange_name = row["orange_team_name"]
        blue_goals = int(row["blue_goals"] or 0)
        orange_goals = int(row["orange_goals"] or 0)
        context = _match_power_context(row, latest_match_dt=latest_match_dt)
        blue = _team_state(teams, blue_name, resolver=resolver)
        orange = _team_state(teams, orange_name, resolver=resolver)
        blue_pre = float(blue["elo"])
        orange_pre = float(orange["elo"])
        expected_blue = 1.0 / (1.0 + 10 ** ((orange_pre - blue_pre) / 400.0))
        actual_blue = 1.0 if blue_goals > orange_goals else 0.0 if blue_goals < orange_goals else 0.5
        delta = (18.0 * context["match_weight"] * (actual_blue - expected_blue)) + (context["telemetry_edge_blue"] * 6.5 * context["recency_weight"])
        blue["elo"] += delta
        orange["elo"] -= delta
        blue["wins"] += int(actual_blue == 1.0)
        blue["losses"] += int(actual_blue == 0.0)
        orange["wins"] += int(actual_blue == 0.0)
        orange["losses"] += int(actual_blue == 1.0)
        blue["games"] += 1
        orange["games"] += 1
        blue["weighted_games"] += context["match_weight"]
        orange["weighted_games"] += context["match_weight"]
        goal_diff = blue_goals - orange_goals
        blue["goal_diff_total"] += goal_diff * context["match_weight"]
        orange["goal_diff_total"] -= goal_diff * context["match_weight"]
        blue["schedule_strength_total"] += orange_pre * context["recency_weight"]
        orange["schedule_strength_total"] += blue_pre * context["recency_weight"]
        blue["quality_total"] += context["telemetry_edge_blue"] * context["recency_weight"]
        orange["quality_total"] -= context["telemetry_edge_blue"] * context["recency_weight"]
        blue["tier_total"] += context["tier_bonus"]
        orange["tier_total"] += context["tier_bonus"]
        blue["result_edge_total"] += (actual_blue - expected_blue) * context["match_weight"]
        orange["result_edge_total"] += ((1.0 - actual_blue) - (1.0 - expected_blue)) * context["match_weight"]
        blue["delta_history"].append(delta)
        orange["delta_history"].append(-delta)
        blue["delta_history"] = blue["delta_history"][-6:]
        orange["delta_history"] = orange["delta_history"][-6:]
        blue["last_delta"] = round(delta, 2)
        orange["last_delta"] = round(-delta, 2)
        if match_date != datetime.min.replace(tzinfo=timezone.utc):
            blue["last_match_date"] = match_date
            orange["last_match_date"] = match_date
        blue["last_replay_id"] = replay_id
        orange["last_replay_id"] = replay_id
    leaderboard_rows = _leaderboard_rows(con)
    leaderboard_region_max_points: dict[str, int] = {}
    leaderboard_region_team_count: dict[str, int] = {}
    for standings in leaderboard_rows:
        region_key = str(standings.get("region") or standings.get("board_name") or "global")
        points = int(standings.get("points") or 0)
        leaderboard_region_max_points[region_key] = max(points, leaderboard_region_max_points.get(region_key, 0))
        leaderboard_region_team_count[region_key] = leaderboard_region_team_count.get(region_key, 0) + 1
    for standings in leaderboard_rows:
        team = _team_state(teams, standings["team_name"], resolver=resolver)
        candidate_rank = standings["rank"]
        candidate_points = standings["points"]
        current_rank = team.get("standings_rank")
        current_points = team.get("standings_points") or 0
        candidate_weight = _standings_stage_weight(standings)
        current_weight = float(team.get("_standings_stage_weight") or 0.0)
        region_key = str(standings.get("region") or standings.get("board_name") or "global")
        if (
            current_rank is None
            or (candidate_weight * candidate_points) > (current_weight * current_points)
            or ((candidate_weight * candidate_points) == (current_weight * current_points) and candidate_rank < current_rank)
        ):
            team["standings_points"] = candidate_points
            team["standings_rank"] = candidate_rank
            team["standings_region"] = standings["region"]
            team["_standings_stage_weight"] = candidate_weight
            team["_standings_region_max_points"] = leaderboard_region_max_points.get(region_key, candidate_points)
            team["_standings_region_team_count"] = leaderboard_region_team_count.get(region_key, 1)
    for team in teams.values():
        games = int(team["games"])
        points = int(team.get("standings_points") or 0)
        weighted_games = float(team["weighted_games"] or 0.0)
        win_rate = (team["wins"] / games) if games else 0.5
        sample_scale = min(1.0, max(weighted_games, 0.0) / 4.0) if games else 0.0
        standings_activity_scale = 0.35 if not games else min(1.0, max(games, 0) / 5.0)
        standings_bonus = _standings_score(team) * standings_activity_scale
        avg_goal_diff = (team["goal_diff_total"] / weighted_games) if weighted_games else 0.0
        schedule_strength = (team["schedule_strength_total"] / max(team["games"], 1)) if games else 1500.0
        schedule_score = max(-70.0, min(70.0, (schedule_strength - 1500.0) * 0.18)) if games else 0.0
        recent_form = (sum(team["delta_history"]) / len(team["delta_history"])) if team["delta_history"] else 0.0
        form_score = max(-55.0, min(55.0, recent_form * 2.2))
        dominance_score = max(
            -125.0,
            min(
                125.0,
                (avg_goal_diff * 18.0) + (float(team["result_edge_total"]) * 18.0) + ((win_rate - 0.5) * 34.0 if games else 0.0),
            ),
        )
        quality_edge = (team["quality_total"] / max(games, 1)) if games else 0.0
        quality_score = max(-26.0, min(26.0, quality_edge * 18.0))
        tier_score = max(-18.0, min(34.0, (float(team["tier_total"]) / max(games, 1)) if games else 0.0))
        confidence = min(
            1.0,
            0.14
            + min(games, 18) / 18.0 * 0.44
            + min(points, 54) / 54.0 * 0.24
            + min(abs(quality_edge), 1.0) * 0.12
            + min(abs(recent_form), 32.0) / 32.0 * 0.06,
        )
        if games:
            schedule_score *= max(0.4, sample_scale)
            form_score *= max(0.35, sample_scale)
            dominance_score *= sample_scale
            quality_score *= max(0.35, sample_scale)
            tier_score *= max(0.35, sample_scale)
        team["win_rate"] = round(win_rate, 4)
        team["confidence"] = round(confidence, 4)
        team["source_count"] = games + (1 if points else 0)
        team["power_score"] = round(team["elo"], 1)
        team["standings_score"] = round(standings_bonus, 1)
        team["schedule_score"] = round(schedule_score, 1)
        team["form_score"] = round(form_score, 1)
        team["dominance_score"] = round(dominance_score, 1)
        team["quality_score"] = round(quality_score, 1)
        team["tier_score"] = round(tier_score, 1)
        team["avg_goal_diff"] = round(avg_goal_diff, 3)
        team["strength_of_schedule"] = round(schedule_strength, 1)
        team["recent_form"] = round(recent_form, 2)
        rating = (
            team["power_score"]
            + team["standings_score"]
            + team["schedule_score"]
            + team["form_score"]
            + team["dominance_score"]
            + team["quality_score"]
            + team["tier_score"]
        )
        if not games:
            rating = team["power_score"] + min(team["standings_score"], 90.0)
        team["rating"] = round(rating, 1)
    ladder = sorted(
        teams.values(),
        key=lambda row: (
            int(row.get("games") or 0) > 0,
            row["rating"],
            row["confidence"],
            row["elo"],
        ),
        reverse=True,
    )
    _reset_team_elo_cache(con)
    if ladder:
        con.executemany(
            """
            INSERT INTO team_elo_cache (
                team_name,
                rating,
                elo,
                power_score,
                standings_score,
                schedule_score,
                form_score,
                dominance_score,
                quality_score,
                tier_score,
                standings_points,
                standings_rank,
                standings_region,
                wins,
                losses,
                games,
                win_rate,
                confidence,
                source_count,
                last_delta,
                avg_goal_diff,
                strength_of_schedule,
                recent_form,
                last_match_date,
                last_replay_id,
                source_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                [
                    row["team_name"],
                    row["rating"],
                    round(row["elo"], 1),
                    row.get("power_score"),
                    row.get("standings_score"),
                    row.get("schedule_score"),
                    row.get("form_score"),
                    row.get("dominance_score"),
                    row.get("quality_score"),
                    row.get("tier_score"),
                    row.get("standings_points") or 0,
                    row.get("standings_rank"),
                    row.get("standings_region"),
                    row["wins"],
                    row["losses"],
                    row["games"],
                    row.get("win_rate"),
                    row.get("confidence"),
                    row.get("source_count"),
                    row["last_delta"],
                    row.get("avg_goal_diff"),
                    row.get("strength_of_schedule"),
                    row.get("recent_form"),
                    row["last_match_date"],
                    row["last_replay_id"],
                    source_key,
                ]
                for row in ladder
            ],
        )
    con.execute(
        "INSERT OR REPLACE INTO site_cache_meta VALUES (?, ?, ?)",
        ["team_elo", source_key, datetime.now(timezone.utc)],
    )


def _team_elo_source_key(con: duckdb.DuckDBPyConnection) -> str:
    latest_run = None
    if table_exists(con, "remote_sync_runs"):
        row = con.execute(
            """
            SELECT run_id
            FROM remote_sync_runs
            WHERE source = 'ballchasing' AND status = 'completed'
            ORDER BY completed_at DESC NULLS LAST, started_at DESC NULLS LAST
            LIMIT 1
            """
        ).fetchone()
        latest_run = row[0] if row else None
    remote_stats = _table_stats(con, "remote_replays", time_column="match_date", synced_column="synced_at")
    parsed_stats = _table_stats(con, "replay_parsed_status", time_column="parsed_at", filters="WHERE status = 'completed'")
    leaderboard_stats = _table_stats(con, "live_leaderboards", time_column="updated_at")
    return json.dumps(
        {
            "cache_version": TEAM_ELO_CACHE_VERSION,
            "latest_run": latest_run,
            "remote": remote_stats,
            "parsed": parsed_stats,
            "leaderboards": leaderboard_stats,
        },
        sort_keys=True,
    )


def replay_review_status(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    ensure_replay_review_cache_schema(con)
    candidates = _review_candidate_rows(con)
    candidate_ids = {row["replay_id"] for row in candidates}
    cached_rows = _safe_fetch(
        con,
        "replay_review_cache",
        """
        SELECT replay_id, computed_at
        FROM replay_review_cache
        ORDER BY computed_at DESC NULLS LAST
        """,
    )
    cached_ids = {row["replay_id"] for row in cached_rows if row.get("replay_id") in candidate_ids}
    return {
        "eligible_replays": len(candidate_ids),
        "cached_replays": len(cached_ids),
        "missing_replays": max(0, len(candidate_ids) - len(cached_ids)),
        "stale_replays": max(0, len(cached_rows) - len(cached_ids)),
        "cache_version": REPLAY_REVIEW_CACHE_VERSION,
        "last_computed_at": cached_rows[0]["computed_at"] if cached_rows else None,
    }


def refresh_replay_review_cache(
    con: duckdb.DuckDBPyConnection,
    *,
    replay_ids: list[str] | None = None,
    limit: int = 50,
    force: bool = False,
) -> dict[str, Any]:
    ensure_replay_review_cache_schema(con)
    prune_candidate_ids = None
    if replay_ids is None:
        prune_candidate_ids = {row["replay_id"] for row in _review_candidate_rows(con, replay_ids=None, limit=None)}
    candidates = _review_candidate_rows(con, replay_ids=replay_ids, limit=limit)
    computed = 0
    cached = 0
    skipped = 0
    for candidate in candidates:
        payload, was_computed = _ensure_replay_review(
            con,
            candidate["replay_id"],
            duration=candidate.get("duration"),
            force=force,
        )
        if payload is None:
            skipped += 1
        elif was_computed:
            computed += 1
        else:
            cached += 1
    pruned = 0
    if replay_ids is None:
        pruned = _prune_replay_review_cache(con, prune_candidate_ids or set())
    status = replay_review_status(con)
    return {
        "requested": len(replay_ids or []),
        "processed": len(candidates),
        "computed": computed,
        "cached": cached,
        "skipped": skipped,
        "pruned": pruned,
        "status": status,
    }


def _prune_replay_review_cache(con: duckdb.DuckDBPyConnection, candidate_ids: set[str]) -> int:
    ensure_replay_review_cache_schema(con)
    before = _count(con, "replay_review_cache")
    if not candidate_ids:
        con.execute("DELETE FROM replay_review_cache")
        return before
    placeholders = ", ".join("?" for _ in candidate_ids)
    con.execute(
        f"DELETE FROM replay_review_cache WHERE replay_id NOT IN ({placeholders})",
        list(candidate_ids),
    )
    return max(0, before - _count(con, "replay_review_cache"))


def _review_candidate_rows(
    con: duckdb.DuckDBPyConnection,
    *,
    replay_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    sources: list[str] = []
    if table_exists(con, "replay_parsed_status"):
        sources.append("SELECT replay_id FROM replay_parsed_status WHERE status = 'completed'")
    if table_exists(con, "events"):
        sources.append("SELECT DISTINCT replay_id FROM events")
    if not sources:
        return []
    joins: list[str] = []
    duration_terms: list[str] = []
    if table_exists(con, "replay_parsed_status"):
        joins.append("LEFT JOIN replay_parsed_status ps USING (replay_id)")
        duration_terms.append("ps.duration_seconds")
    else:
        joins.append("LEFT JOIN (SELECT NULL AS replay_id, NULL AS duration_seconds, NULL AS parsed_at) ps USING (replay_id)")
    if table_exists(con, "remote_replays"):
        joins.append("LEFT JOIN remote_replays rr USING (replay_id)")
        duration_terms.append("rr.duration")
    else:
        joins.append("LEFT JOIN (SELECT NULL AS replay_id, NULL AS duration, NULL AS match_date) rr USING (replay_id)")
    if table_exists(con, "replays"):
        joins.append("LEFT JOIN replays r USING (replay_id)")
        duration_terms.append("r.game_duration")
    else:
        joins.append("LEFT JOIN (SELECT NULL AS replay_id, NULL AS game_duration, NULL AS ingested_at) r USING (replay_id)")
    params: list[Any] = []
    where = ""
    if replay_ids:
        placeholders = ", ".join("?" for _ in replay_ids)
        where = f"WHERE cid.replay_id IN ({placeholders})"
        params.extend(replay_ids)
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(limit)
    return _fetch(
        con,
        f"""
        WITH candidate_ids AS (
            {' UNION '.join(sources)}
        )
        SELECT
            cid.replay_id,
            COALESCE({', '.join(duration_terms + ['300.0'])}) AS duration
        FROM candidate_ids cid
        {' '.join(joins)}
        {where}
        ORDER BY ps.parsed_at DESC NULLS LAST, rr.match_date DESC NULLS LAST, r.ingested_at DESC NULLS LAST, cid.replay_id
        {limit_clause}
        """,
        params,
    )


def _review_current_source_key(con: duckdb.DuckDBPyConnection, replay_id: str) -> str:
    parsed_status = None
    parsed_events = None
    warehouse_events = None
    predictions = None
    if table_exists(con, "replay_parsed_status"):
        row = con.execute(
            """
            SELECT status, parsed_at, duration_seconds, frame_count, target_hz
            FROM replay_parsed_status
            WHERE replay_id = ?
            """,
            [replay_id],
        ).fetchone()
        if row:
            parsed_status = {
                "status": row[0],
                "parsed_at": row[1].isoformat() if row[1] else None,
                "duration_seconds": row[2],
                "frame_count": row[3],
                "target_hz": row[4],
            }
    if table_exists(con, "replay_parsed_events"):
        row = con.execute(
            """
            SELECT COUNT(*), MAX(event_id), MAX(t)
            FROM replay_parsed_events
            WHERE replay_id = ?
            """,
            [replay_id],
        ).fetchone()
        parsed_events = {"count": int(row[0] or 0), "max_event_id": row[1], "max_t": row[2]}
    if table_exists(con, "events"):
        row = con.execute(
            """
            SELECT COUNT(*), MAX(event_id), MAX(t)
            FROM events
            WHERE replay_id = ?
            """,
            [replay_id],
        ).fetchone()
        warehouse_events = {"count": int(row[0] or 0), "max_event_id": row[1], "max_t": row[2]}
    if table_exists(con, "predictions"):
        row = con.execute(
            """
            SELECT COUNT(*), MAX(created_at), MAX(probability), MAX(score)
            FROM predictions
            WHERE replay_id = ?
            """,
            [replay_id],
        ).fetchone()
        predictions = {
            "count": int(row[0] or 0),
            "max_created_at": row[1].isoformat() if row[1] else None,
            "max_probability": row[2],
            "max_score": row[3],
        }
    return json.dumps(
        {
            "version": REPLAY_REVIEW_CACHE_VERSION,
            "parsed_status": parsed_status,
            "parsed_events": parsed_events,
            "warehouse_events": warehouse_events,
            "predictions": predictions,
        },
        sort_keys=True,
    )


def _review_predictions(con: duckdb.DuckDBPyConnection, replay_id: str) -> list[dict[str, Any]]:
    rows = _safe_fetch(
        con,
        "predictions",
        """
        SELECT prediction_type, predicted_label, probability, score, reasons_json, created_at
        FROM predictions
        WHERE replay_id = ?
        ORDER BY created_at DESC
        """,
        [replay_id],
    )
    for prediction in rows:
        prediction["reasons"] = _parse_json(prediction.pop("reasons_json", None), [])
    return rows


def _review_events(con: duckdb.DuckDBPyConnection, replay_id: str) -> list[dict[str, Any]]:
    rows = parsed_events(con, replay_id)
    if not rows:
        rows = _safe_fetch(
            con,
            "events",
            """
            SELECT event_id, replay_id, t, event_type, team_color, team_id, player_id, player_name,
                   other_team_color, other_team_id, other_player_id, other_player_name, value, meta
            FROM events
            WHERE replay_id = ?
            ORDER BY t, event_id
            """,
            [replay_id],
        )
    if rows:
        resolver = IdentityResolver(con)
        for event in rows:
            if event.get("player_name") or event.get("player_id"):
                event["player_name"] = resolver.resolve_player(event.get("player_id"), event.get("player_name"))["player_name"]
            if event.get("other_player_name") or event.get("other_player_id"):
                event["other_player_name"] = resolver.resolve_player(event.get("other_player_id"), event.get("other_player_name"))["player_name"]
    return rows


def _review_duration(con: duckdb.DuckDBPyConnection, replay_id: str) -> float:
    candidates = _review_candidate_rows(con, replay_ids=[replay_id], limit=1)
    if candidates:
        return float(candidates[0].get("duration") or 300.0)
    return 300.0


def _ensure_replay_review(
    con: duckdb.DuckDBPyConnection,
    replay_id: str,
    *,
    duration: float | None = None,
    events: list[dict[str, Any]] | None = None,
    predictions: list[dict[str, Any]] | None = None,
    force: bool = False,
) -> tuple[dict[str, Any] | None, bool]:
    ensure_replay_review_cache_schema(con)
    source_key = _review_current_source_key(con, replay_id)
    cached_rows = _safe_fetch(
        con,
        "replay_review_cache",
        "SELECT * FROM replay_review_cache WHERE replay_id = ?",
        [replay_id],
    )
    cached_row = cached_rows[0] if cached_rows else None
    if cached_row and cached_row.get("source_key") == source_key and not force:
        return _decode_replay_review_row(cached_row), False
    events = events if events is not None else _review_events(con, replay_id)
    if not events:
        return (_decode_replay_review_row(cached_row) if cached_row else None), False
    predictions = predictions if predictions is not None else _review_predictions(con, replay_id)
    actual_duration = float(duration or _review_duration(con, replay_id) or 300.0)
    payload = _build_replay_review_payload(events, actual_duration, predictions)
    try:
        _store_replay_review(con, replay_id, source_key, actual_duration, len(events), payload)
    except duckdb.Error:
        pass
    return payload, True


def _store_replay_review(
    con: duckdb.DuckDBPyConnection,
    replay_id: str,
    source_key: str,
    duration: float,
    event_count: int,
    payload: dict[str, Any],
) -> None:
    eval_payload = payload["eval"]
    summary = payload["summary"]
    largest_blunder = summary.get("largest_blunder") or {}
    best_play = summary.get("best_play") or {}
    clutch_play = summary.get("clutch_play") or {}
    turning_point = summary.get("turning_point") or {}
    con.execute(
        """
        INSERT OR REPLACE INTO replay_review_cache (
            replay_id, source_key, computed_at, duration, event_count,
            base_blue_probability, final_blue_probability, volatility, swing_count,
            largest_blunder_player_name, largest_blunder_label, largest_blunder_impact, largest_blunder_t,
            best_play_player_name, best_play_label, best_play_impact, best_play_t,
            clutch_play_player_name, clutch_play_label, clutch_play_impact, clutch_play_t,
            turning_point_label, turning_point_t, turning_point_event_type,
            blue_net, orange_net, eval_json, win_edge_json, player_impact_json, turning_points_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            replay_id,
            source_key,
            datetime.now(timezone.utc),
            duration,
            event_count,
            eval_payload.get("base_blue_probability"),
            eval_payload.get("final_blue_probability"),
            eval_payload.get("volatility"),
            eval_payload.get("swing_count"),
            largest_blunder.get("player_name"),
            largest_blunder.get("label"),
            largest_blunder.get("impact"),
            largest_blunder.get("t"),
            best_play.get("player_name"),
            best_play.get("label"),
            best_play.get("impact"),
            best_play.get("t"),
            clutch_play.get("player_name"),
            clutch_play.get("label"),
            clutch_play.get("impact"),
            clutch_play.get("t"),
            turning_point.get("label"),
            turning_point.get("t"),
            turning_point.get("event_type"),
            eval_payload.get("team_net", {}).get("blue"),
            eval_payload.get("team_net", {}).get("orange"),
            json.dumps(eval_payload, separators=(",", ":"), ensure_ascii=True),
            json.dumps(payload["win_edge"], separators=(",", ":"), ensure_ascii=True),
            json.dumps(payload["player_impact"], separators=(",", ":"), ensure_ascii=True),
            json.dumps(payload["turning_points"], separators=(",", ":"), ensure_ascii=True),
        ],
    )


def _decode_replay_review_row(row: dict[str, Any]) -> dict[str, Any]:
    eval_payload = _parse_json(row.get("eval_json"), {})
    win_edge = _parse_json(row.get("win_edge_json"), {"segments": [], "highlights": [], "base_probability": 0.5})
    player_impact = _parse_json(row.get("player_impact_json"), [])
    turning_points = _parse_json(row.get("turning_points_json"), [])
    summary = {
        "replay_id": row.get("replay_id"),
        "base_blue_probability": row.get("base_blue_probability"),
        "final_blue_probability": row.get("final_blue_probability"),
        "volatility": row.get("volatility"),
        "volatility_points": eval_payload.get("volatility_points"),
        "swing_count": row.get("swing_count"),
        "largest_blunder": _summary_event_row(
            row.get("largest_blunder_player_name"),
            row.get("largest_blunder_label"),
            row.get("largest_blunder_impact"),
            row.get("largest_blunder_t"),
        ),
        "best_play": _summary_event_row(
            row.get("best_play_player_name"),
            row.get("best_play_label"),
            row.get("best_play_impact"),
            row.get("best_play_t"),
        ),
        "clutch_play": _summary_event_row(
            row.get("clutch_play_player_name"),
            row.get("clutch_play_label"),
            row.get("clutch_play_impact"),
            row.get("clutch_play_t"),
        ),
        "turning_point": {
            "label": row.get("turning_point_label"),
            "t": row.get("turning_point_t"),
            "event_type": row.get("turning_point_event_type"),
        } if row.get("turning_point_label") else None,
        "impact_leader": player_impact[0] if player_impact else None,
    }
    return {
        "summary": summary,
        "eval": eval_payload,
        "win_edge": win_edge,
        "player_impact": player_impact,
        "turning_points": turning_points,
    }


def _summary_event_row(player_name: Any, label: Any, impact: Any, t_value: Any) -> dict[str, Any] | None:
    if player_name is None and label is None and impact is None and t_value is None:
        return None
    return {
        "player_name": player_name,
        "label": label,
        "impact": impact,
        "t": t_value,
    }


def _build_replay_review_payload(
    events: list[dict[str, Any]],
    duration: float,
    predictions: list[dict[str, Any]],
) -> dict[str, Any]:
    win_edge = build_win_edge(events, duration, predictions)
    replay_eval = build_replay_eval(events, predictions, duration=duration)
    turning_points = build_replay_timeline(events).get("turning_points", [])[:8]
    player_impact = _build_player_impact(events, replay_eval, duration)
    summary = {
        "base_blue_probability": replay_eval.get("base_blue_probability"),
        "final_blue_probability": replay_eval.get("final_blue_probability"),
        "volatility": replay_eval.get("volatility"),
        "volatility_points": replay_eval.get("volatility_points"),
        "swing_count": replay_eval.get("swing_count"),
        "largest_blunder": replay_eval.get("blunders", [None])[0] if replay_eval.get("blunders") else None,
        "best_play": replay_eval.get("plays", [None])[0] if replay_eval.get("plays") else None,
        "clutch_play": replay_eval.get("clutch_plays", [None])[0] if replay_eval.get("clutch_plays") else None,
        "turning_point": turning_points[0] if turning_points else None,
        "impact_leader": player_impact[0] if player_impact else None,
    }
    return {
        "summary": summary,
        "win_edge": win_edge,
        "eval": replay_eval,
        "player_impact": player_impact,
        "turning_points": turning_points,
    }


def _attach_replay_review_summaries(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        replay_id = row.get("replay_id")
        if not replay_id:
            continue
        payload, _ = _ensure_replay_review(con, replay_id, duration=row.get("duration"))
        if payload:
            row["review"] = payload["summary"]


def _build_player_impact(events: list[dict[str, Any]], replay_eval: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    ordered = sorted(events, key=lambda row: (float(row.get("t") or 0.0), int(row.get("event_id") or 0)))
    net_by_player = {
        row["player_name"]: {
            "edge_score": float(row.get("impact") or 0.0),
            "probability_swing": float(row.get("net_probability_swing") or 0.0),
            "swing_points": float(row.get("net_swing_points") or 0.0),
        }
        for row in replay_eval.get("player_net", [])
        if row.get("player_name")
    }
    counts: dict[str, dict[str, Any]] = {}
    clutch_cutoff = duration * 0.75 if duration else (ordered[-1].get("t") or 0.0) * 0.75 if ordered else 0.0
    major_swing_threshold = 1.5
    base_probability = float(replay_eval.get("base_blue_probability") or 0.5)
    score = math.log(base_probability / max(1e-6, 1.0 - base_probability))
    for event in ordered:
        team_color = event.get("team_color")
        player_name = event.get("player_name")
        event_type = event.get("event_type")
        actor_name = player_name
        actor_team = team_color
        delta = _edge_delta(event)
        actor_edge = delta
        before = 1.0 / (1.0 + math.exp(-max(min(score, 8.0), -8.0)))
        score += delta
        after = 1.0 / (1.0 + math.exp(-max(min(score, 8.0), -8.0)))
        actor_probability_swing = (after - before) if team_color == "blue" else (before - after) if team_color == "orange" else 0.0
        if event_type == "turnover" and event.get("other_team_color"):
            actor_name = event.get("other_player_name") or event.get("other_player_id") or actor_name
            actor_team = event.get("other_team_color")
            actor_edge = -abs(delta)
            actor_probability_swing = -abs(after - before)
        if actor_name:
            bucket = counts.setdefault(
                actor_name,
                {
                    "player_name": actor_name,
                    "team_color": actor_team,
                    "touches": 0,
                    "goals": 0,
                    "demos": 0,
                    "pressure_phases": 0,
                    "turnovers_forced": 0,
                    "turnovers_committed": 0,
                    "clutch_impact": 0.0,
                    "clutch_swing_points": 0.0,
                    "positive_swings": 0,
                    "negative_swings": 0,
                },
            )
            bucket["team_color"] = bucket.get("team_color") or actor_team
            swing_points = actor_probability_swing * 100.0
            if abs(swing_points) >= major_swing_threshold:
                if swing_points >= 0:
                    bucket["positive_swings"] += 1
                else:
                    bucket["negative_swings"] += 1
            if float(event.get("t") or 0.0) >= clutch_cutoff:
                bucket["clutch_impact"] += actor_probability_swing
                bucket["clutch_swing_points"] += swing_points
        if player_name:
            direct = counts.setdefault(
                player_name,
                {
                    "player_name": player_name,
                    "team_color": team_color,
                    "touches": 0,
                    "goals": 0,
                    "demos": 0,
                    "pressure_phases": 0,
                    "turnovers_forced": 0,
                    "turnovers_committed": 0,
                    "clutch_impact": 0.0,
                    "clutch_swing_points": 0.0,
                    "positive_swings": 0,
                    "negative_swings": 0,
                },
            )
            direct["team_color"] = direct.get("team_color") or team_color
            if event_type == "touch":
                direct["touches"] += 1
            elif event_type == "goal":
                direct["goals"] += 1
            elif event_type == "demo":
                direct["demos"] += 1
            elif event_type == "pressure_phase":
                direct["pressure_phases"] += 1
            elif event_type == "turnover":
                direct["turnovers_forced"] += 1
        if event_type == "turnover" and event.get("other_player_name"):
            committed = counts.setdefault(
                event["other_player_name"],
                {
                    "player_name": event["other_player_name"],
                    "team_color": event.get("other_team_color"),
                    "touches": 0,
                    "goals": 0,
                    "demos": 0,
                    "pressure_phases": 0,
                    "turnovers_forced": 0,
                    "turnovers_committed": 0,
                    "clutch_impact": 0.0,
                    "clutch_swing_points": 0.0,
                    "positive_swings": 0,
                    "negative_swings": 0,
                },
            )
            committed["turnovers_committed"] += 1
    ordered_players = []
    for player_name, impact_row in net_by_player.items():
        bucket = counts.setdefault(
            player_name,
            {
                "player_name": player_name,
                "team_color": None,
                "touches": 0,
                "goals": 0,
                "demos": 0,
                "pressure_phases": 0,
                "turnovers_forced": 0,
                "turnovers_committed": 0,
                "clutch_impact": 0.0,
                "clutch_swing_points": 0.0,
                "positive_swings": 0,
                "negative_swings": 0,
            },
        )
        raw_edge = float(impact_row.get("edge_score") or 0.0)
        probability_swing = float(impact_row.get("probability_swing") or 0.0)
        swing_points = float(impact_row.get("swing_points") or 0.0)
        bucket["net_impact"] = round(raw_edge, 4)
        bucket["net_probability_swing"] = round(probability_swing, 4)
        bucket["net_swing_points"] = round(swing_points, 1)
        bucket["impact_per_touch"] = round(raw_edge / max(bucket["touches"], 1), 4)
        bucket["advantage_per_touch_points"] = round(swing_points / max(bucket["touches"], 1), 1)
        bucket["clutch_impact"] = round(float(bucket["clutch_impact"]), 4)
        bucket["clutch_swing_points"] = round(float(bucket.get("clutch_swing_points") or 0.0), 1)
        ordered_players.append(bucket)
    ordered_players.sort(
        key=lambda item: (
            float(item.get("net_swing_points") or 0.0),
            float(item.get("clutch_swing_points") or 0.0),
            int(item.get("goals") or 0),
            -int(item.get("turnovers_committed") or 0),
        ),
        reverse=True,
    )
    return ordered_players[:8]


def replay_viewer(con: duckdb.DuckDBPyConnection, replay_id: str) -> dict[str, Any] | None:
    replay = get_library_replay(con, replay_id)
    if replay is None:
        return None
    events: list[dict[str, Any]] = []
    if not replay.get("local_file_path") and replay.get("source") == "ballchasing":
        try:
            ensure_ballchasing_replay_download(replay_id, parse_download=True)
            with database_connection(get_settings().serving_db, read_only=True) as fresh_con:
                refreshed = get_library_replay(fresh_con, replay_id)
                if refreshed is not None:
                    replay = refreshed
        except Exception:
            pass
    if replay.get("local_file_path"):
        try:
            ensure_replay_analysis(replay_id, local_file_path=replay.get("local_file_path"))
            with database_connection(get_settings().serving_db, read_only=True) as fresh_con:
                refreshed = get_library_replay(fresh_con, replay_id)
                if refreshed is not None:
                    replay = refreshed
                events = parsed_events(fresh_con, replay_id)
        except Exception:
            events = []
    if not events:
        events = _safe_fetch(
            con,
            "events",
            """
            SELECT event_id, replay_id, t, event_type, team_color, team_id, player_id, player_name,
                   other_team_color, other_team_id, other_player_id, other_player_name, value, meta
            FROM events
            WHERE replay_id = ?
            ORDER BY t, event_id
            """,
            [replay_id],
        )
    if not events and replay.get("local_file_path"):
        with database_connection(get_settings().serving_db, read_only=True) as fresh_con:
            events = parsed_events(fresh_con, replay_id)
    if events:
        resolver = IdentityResolver(con)
        for event in events:
            if event.get("player_name") or event.get("player_id"):
                event["player_name"] = resolver.resolve_player(event.get("player_id"), event.get("player_name"))["player_name"]
            if event.get("other_player_name") or event.get("other_player_id"):
                event["other_player_name"] = resolver.resolve_player(event.get("other_player_id"), event.get("other_player_name"))["player_name"]
    predictions = _safe_fetch(
        con,
        "predictions",
        """
        SELECT prediction_type, predicted_label, probability, score, reasons_json
        FROM predictions
        WHERE replay_id = ?
        ORDER BY created_at DESC
        """,
        [replay_id],
    )
    for prediction in predictions:
        prediction["reasons"] = _parse_json(prediction.pop("reasons_json", None), [])
    timeline = build_replay_timeline(events)
    review_payload, _ = _ensure_replay_review(
        con,
        replay_id,
        duration=replay.get("duration") or replay.get("game_duration") or 300.0,
        events=events,
        predictions=predictions,
    )
    edge = review_payload["win_edge"] if review_payload else build_win_edge(events, replay.get("duration") or replay.get("game_duration") or 300.0, predictions)
    replay_eval = review_payload["eval"] if review_payload else build_replay_eval(events, predictions, duration=replay.get("duration") or replay.get("game_duration") or 300.0)
    return {
        "replay": replay,
        "events": events[:120],
        "timeline": {
            **timeline,
            "turning_points": review_payload["turning_points"] if review_payload else timeline.get("turning_points", []),
        },
        "win_edge": edge,
        "eval": replay_eval,
        "player_impact": review_payload["player_impact"] if review_payload else _build_player_impact(events, replay_eval, float(replay.get("duration") or replay.get("game_duration") or 300.0)),
        "predictions": predictions,
    }


def build_win_edge(events: list[dict[str, Any]], duration: float, predictions: list[dict[str, Any]], *, segments: int = 42) -> dict[str, Any]:
    duration = float(duration or 1.0)
    base_probability = 0.5
    for prediction in predictions:
        if prediction.get("prediction_type") == "blue_win_probability" and prediction.get("probability") is not None:
            base_probability = float(prediction["probability"])
            break
    base_score = math.log(base_probability / max(1e-6, 1.0 - base_probability))
    ordered = sorted(events, key=lambda row: float(row.get("t") or 0.0))
    cursor = 0
    score = base_score
    points: list[dict[str, Any]] = []
    highlights: list[dict[str, Any]] = []
    for index in range(segments):
        end_t = duration * (index + 1) / segments
        while cursor < len(ordered) and float(ordered[cursor].get("t") or 0.0) <= end_t:
            event = ordered[cursor]
            before = 1.0 / (1.0 + math.exp(-max(min(score, 8.0), -8.0)))
            delta = _edge_delta(event)
            score += delta
            after = 1.0 / (1.0 + math.exp(-max(min(score, 8.0), -8.0)))
            probability_swing = after - before
            if abs(probability_swing) >= 0.025 or (event.get("event_type") in {"goal", "turnover", "pressure_phase", "demo"} and abs(probability_swing) >= 0.012):
                highlights.append(
                    {
                        "t": round(float(event.get("t") or 0.0), 1),
                        "event_type": event.get("event_type"),
                        "team_color": event.get("team_color"),
                        "swing": round(probability_swing, 4),
                        "swing_points": round(probability_swing * 100.0, 1),
                    }
                )
            cursor += 1
        prob = 1.0 / (1.0 + math.exp(-max(min(score, 8.0), -8.0)))
        points.append(
            {
                "bucket": index,
                "end_t": round(end_t, 1),
                "blue_probability": round(prob, 4),
                "blue_edge": round(prob - 0.5, 4),
                "leader": "blue" if prob >= 0.5 else "orange",
            }
        )
    return {"segments": points, "highlights": highlights[:8], "base_probability": round(base_probability, 4)}


def build_replay_eval(
    events: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    *,
    duration: float | None = None,
) -> dict[str, Any]:
    details = _replay_eval_details(events, predictions)
    ordered = details["ordered"]
    ledger = details["ledger"]
    player_totals = details["player_totals"]
    player_probability_totals = details["player_probability_totals"]
    team_totals = details["team_totals"]
    score = details["score"]
    base_probability = details["base_probability"]
    effective_duration = float(duration or (ordered[-1].get("t") if ordered else 0.0) or 0.0)
    clutch_cutoff = effective_duration * 0.75 if effective_duration else 0.0
    edge = build_win_edge(events, effective_duration or 1.0, predictions)
    segment_probabilities = [base_probability, *[float(segment.get("blue_probability") or 0.0) for segment in edge.get("segments", [])]]
    volatility_points = round(
        sum(abs(segment_probabilities[index] - segment_probabilities[index - 1]) for index in range(1, len(segment_probabilities))) * 100.0,
        1,
    )
    minor_swing_threshold = 1.5
    major_swing_threshold = 3.0
    blunders = sorted(
        [row for row in ledger if float(row.get("swing_points") or 0.0) <= -minor_swing_threshold],
        key=lambda item: float(item.get("swing_points") or 0.0),
    )[:8]
    plays = sorted(
        [row for row in ledger if float(row.get("swing_points") or 0.0) >= minor_swing_threshold],
        key=lambda item: float(item.get("swing_points") or 0.0),
        reverse=True,
    )[:8]
    clutch_plays = sorted(
        [row for row in plays if row["t"] >= clutch_cutoff],
        key=lambda item: (float(item.get("swing_points") or 0.0), -float(item.get("t") or 0.0)),
        reverse=True,
    )[:6]
    final_probability = 1.0 / (1.0 + math.exp(-max(min(score, 8.0), -8.0)))
    largest_swing = max(ledger, key=lambda row: abs(float(row.get("swing_points") or 0.0)), default=None)
    return {
        "base_blue_probability": round(base_probability, 4),
        "final_blue_probability": round(final_probability, 4),
        "volatility": round(sum(abs(row["impact"]) for row in ledger), 4),
        "volatility_points": volatility_points,
        "swing_count": sum(1 for row in ledger if abs(float(row.get("swing_points") or 0.0)) >= major_swing_threshold),
        "event_count": len(ledger),
        "largest_swing": largest_swing,
        "largest_swing_points": round(abs(float(largest_swing.get("probability_swing") or 0.0)) * 100.0, 1) if largest_swing else None,
        "blunders": blunders,
        "plays": plays,
        "clutch_plays": clutch_plays,
        "player_net": [
            {
                "player_name": name,
                "impact": round(player_totals.get(name, 0.0), 4),
                "net_probability_swing": round(player_probability_totals.get(name, 0.0), 4),
                "net_swing_points": round(player_probability_totals.get(name, 0.0) * 100.0, 1),
            }
            for name, _ in sorted(player_probability_totals.items(), key=lambda item: abs(item[1]), reverse=True)[:10]
        ],
        "team_net": {
            color: round(value * 100.0, 1)
            for color, value in team_totals.items()
        },
    }


def _replay_eval_details(events: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    base_probability = 0.5
    for prediction in predictions:
        if prediction.get("prediction_type") == "blue_win_probability" and prediction.get("probability") is not None:
            base_probability = float(prediction["probability"])
            break
    score = math.log(base_probability / max(1e-6, 1.0 - base_probability))
    ordered = sorted(events, key=lambda row: (float(row.get("t") or 0.0), int(row.get("event_id") or 0)))
    ledger: list[dict[str, Any]] = []
    player_totals: dict[str, float] = {}
    player_probability_totals: dict[str, float] = {}
    team_totals = {"blue": 0.0, "orange": 0.0}
    for event in ordered:
        delta = _edge_delta(event)
        if abs(delta) < 0.02 and event.get("event_type") not in {"goal", "turnover", "overcommit_proxy", "pressure_phase", "kickoff_outcome", "boost_starvation_window"}:
            continue
        team_color = event.get("team_color")
        before = 1.0 / (1.0 + math.exp(-max(min(score, 8.0), -8.0)))
        score += delta
        after = 1.0 / (1.0 + math.exp(-max(min(score, 8.0), -8.0)))
        actor_edge = delta if team_color == "blue" else -delta if team_color == "orange" else 0.0
        actor_probability_swing = (after - before) if team_color == "blue" else (before - after) if team_color == "orange" else 0.0
        actor_player = event.get("player_name")
        actor_team = team_color
        if event.get("event_type") == "turnover" and event.get("other_team_color"):
            actor_edge = -abs(delta)
            actor_probability_swing = -abs(after - before)
            actor_player = event.get("other_player_name") or event.get("other_player_id") or actor_player
            actor_team = event.get("other_team_color")
        label = _eval_label(event, actor_player=actor_player, actor_team=actor_team)
        severity = _impact_severity(abs(actor_edge))
        row = {
            "t": round(float(event.get("t") or 0.0), 1),
            "event_type": event.get("event_type"),
            "team_color": actor_team,
            "player_name": actor_player,
            "label": label,
            "before_blue_probability": round(before, 4),
            "after_blue_probability": round(after, 4),
            "blue_swing": round(delta, 4),
            "impact": round(actor_edge, 4),
            "probability_swing": round(actor_probability_swing, 4),
            "swing_points": round(actor_probability_swing * 100.0, 1),
            "severity": severity,
        }
        ledger.append(row)
        if actor_player:
            player_totals[actor_player] = player_totals.get(actor_player, 0.0) + actor_edge
            player_probability_totals[actor_player] = player_probability_totals.get(actor_player, 0.0) + actor_probability_swing
        if actor_team in team_totals:
            team_totals[actor_team] += actor_probability_swing
    return {
        "base_probability": base_probability,
        "score": score,
        "ordered": ordered,
        "ledger": ledger,
        "player_totals": player_totals,
        "player_probability_totals": player_probability_totals,
        "team_totals": team_totals,
    }


def _edge_delta(event: dict[str, Any]) -> float:
    event_type = event.get("event_type")
    value = float(event.get("value") or 0.0)
    team_color = event.get("team_color")
    sign = 1.0 if team_color == "blue" else -1.0 if team_color == "orange" else 0.0
    meta = _parse_json(event.get("meta"), {}) if isinstance(event.get("meta"), str) else (event.get("meta") or {})
    if event_type == "boost_starvation_window":
        return -sign * 0.2
    if event_type == "overcommit_proxy":
        return -sign * min(0.45, abs(value) * 0.8)
    if event_type == "goal":
        base = 1.45
    elif event_type == "turnover":
        gap_seconds = float(meta.get("gap_seconds") or 0.0)
        shot_bonus = 0.08 if meta.get("shot") else 0.0
        base = 0.12 + min(0.22, gap_seconds * 0.06) + shot_bonus
    elif event_type == "pressure_phase":
        shot_bonus = 0.14 if meta.get("shot") else 0.0
        base = 0.18 + min(0.26, max(0.0, abs(value) - 0.4) * 0.18) + shot_bonus
    elif event_type == "kickoff_outcome":
        base = 0.16 + min(0.08, max(0.0, abs(value) - 1.0) * 0.06)
    elif event_type == "demo":
        base = 0.22
    elif event_type == "touch":
        distance_to_goal = float(meta.get("distance_to_goal") or 0.0)
        shot_bonus = 0.05 if meta.get("shot") else 0.0
        goal_bonus = 0.08 if meta.get("goal") else 0.0
        threatening = 0.03 if distance_to_goal and distance_to_goal < 3200 else 0.0
        base = 0.01 + shot_bonus + goal_bonus + threatening
    elif event_type == "possession_start":
        base = 0.04
    elif event_type == "possession_end":
        base = -0.015
    else:
        base = 0.0
    return sign * base


def _eval_label(event: dict[str, Any], *, actor_player: str | None, actor_team: str | None) -> str:
    team = actor_team or event.get("team_color") or "neutral"
    player = actor_player or event.get("player_name") or "team"
    event_type = event.get("event_type")
    if event_type == "turnover":
        return f"{player} turned possession over"
    if event_type == "goal":
        return f"{player} scored for {team}"
    if event_type == "overcommit_proxy":
        return f"{team} overcommit left the back line open"
    if event_type == "boost_starvation_window":
        return f"{team} got starved on boost"
    if event_type == "pressure_phase":
        return f"{player} created a pressure phase"
    if event_type == "kickoff_outcome":
        return f"{player} won the kickoff edge"
    if event_type == "demo":
        return f"{player} landed a demo"
    return f"{player} {event_type.replace('_', ' ')}"


def _impact_severity(value: float) -> str:
    if value >= 1.0:
        return "massive"
    if value >= 0.45:
        return "major"
    if value >= 0.16:
        return "medium"
    return "minor"


def _apply_parse_status(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
    status_by_replay = parsed_status_map(
        con,
        [row["replay_id"] for row in rows if row.get("replay_id")],
    )
    for row in rows:
        status = status_by_replay.get(row.get("replay_id"))
        row["carball_status"] = status
        if status and status.get("status") == "completed":
            row["has_semantic_features"] = True
            if not row.get("local_file_path") and status.get("local_file_path"):
                row["local_file_path"] = status["local_file_path"]
            if not row.get("duration") and status.get("duration_seconds") is not None:
                row["duration"] = status["duration_seconds"]
            if not row.get("blue_team_name") and status.get("blue_team_name"):
                row["blue_team_name"] = status["blue_team_name"]
            if not row.get("orange_team_name") and status.get("orange_team_name"):
                row["orange_team_name"] = status["orange_team_name"]
            if row.get("blue_goals") is None and status.get("blue_goals") is not None:
                row["blue_goals"] = status["blue_goals"]
            if row.get("orange_goals") is None and status.get("orange_goals") is not None:
                row["orange_goals"] = status["orange_goals"]


def _fill_remote_score_from_raw(replay: dict[str, Any]) -> None:
    raw = replay.get("raw") or {}
    blue_goals = (((raw.get("blue") or {}).get("stats") or {}).get("core") or {}).get("goals")
    orange_goals = (((raw.get("orange") or {}).get("stats") or {}).get("core") or {}).get("goals")
    if replay.get("blue_goals") is None and blue_goals is not None:
        replay["blue_goals"] = int(blue_goals)
    if replay.get("orange_goals") is None and orange_goals is not None:
        replay["orange_goals"] = int(orange_goals)


def _count_completed_parses(con: duckdb.DuckDBPyConnection) -> int:
    if not table_exists(con, "replay_parsed_status"):
        return 0
    return int(
        con.execute(
            "SELECT COUNT(*) FROM replay_parsed_status WHERE status = 'completed'"
        ).fetchone()[0]
    )


def _local_replay_path(replay_id: str) -> str | None:
    settings = get_settings()
    candidates = [
        settings.replay_download_dir / f"{replay_id}.replay",
        settings.replay_download_dir.parent / f"{replay_id}.replay",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _parsed_player_boxscore(con: duckdb.DuckDBPyConnection, replay_id: str) -> list[dict[str, Any]]:
    if not table_exists(con, "replay_parsed_events"):
        return []
    parsed_player_meta: dict[str, dict[str, Any]] = {}
    if table_exists(con, "replay_parsed_frames"):
        row = con.execute(
            "SELECT players_json FROM replay_parsed_frames WHERE replay_id = ?",
            [replay_id],
        ).fetchone()
        for player in _parse_json(row[0] if row else None, []):
            player_id = str(player.get("player_id") or "").strip()
            if player_id:
                parsed_player_meta[player_id] = player
    status_row = con.execute(
        """
        SELECT blue_team_name, orange_team_name
        FROM replay_parsed_status
        WHERE replay_id = ? AND status = 'completed'
        """,
        [replay_id],
    ).fetchone()
    team_name_map = {
        "blue": status_row[0] if status_row and status_row[0] else "Blue Side",
        "orange": status_row[1] if status_row and status_row[1] else "Orange Side",
    }
    player_rows = con.execute(
        """
        SELECT
            player_id,
            any_value(player_name) AS player_name,
            any_value(team_color) AS team_color,
            SUM(CASE WHEN event_type = 'goal' THEN 1 ELSE 0 END) AS goals,
            SUM(CASE WHEN event_type = 'touch' THEN 1 ELSE 0 END) AS touches,
            SUM(CASE WHEN event_type = 'demo' THEN 1 ELSE 0 END) AS demos,
            SUM(CASE WHEN event_type = 'pressure_phase' THEN 1 ELSE 0 END) AS pressure_events,
            SUM(CASE WHEN event_type = 'possession_start' THEN 1 ELSE 0 END) AS possessions
        FROM replay_parsed_events
        WHERE replay_id = ? AND player_name IS NOT NULL
        GROUP BY player_id
        ORDER BY any_value(team_color), goals DESC, touches DESC, player_name
        """,
        [replay_id],
    ).fetchall()
    rows = [
        {
            "side": row[2],
            "platform": None,
            "platform_player_id": row[0],
            "player_name": row[1],
            "car_name": (parsed_player_meta.get(str(row[0])) or {}).get("car_name"),
            "score": int((row[3] or 0) * 100 + (row[4] or 0) * 4 + (row[5] or 0) * 15 + (row[6] or 0) * 10 + (row[7] or 0) * 6),
            "goals": row[3] or 0,
            "assists": None,
            "saves": None,
            "shots": None,
            "demos_inflicted": row[5] or 0,
            "team_name": team_name_map.get(row[2] or "blue"),
            "touches": row[4] or 0,
        }
        for row in player_rows
    ]
    return _canonicalize_boxscores(con, rows)


def _power_match_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if table_exists(con, "remote_replays"):
        remote_columns = _table_columns(con, "remote_replays")
        if {"match_date", "created_at"}.issubset(remote_columns):
            match_date_expr = "COALESCE(match_date, created_at)"
        elif "match_date" in remote_columns:
            match_date_expr = "match_date"
        elif "created_at" in remote_columns:
            match_date_expr = "created_at"
        else:
            match_date_expr = "NULL"
        rows.extend(
            rows_to_dicts(
                con.execute(
                f"""
                SELECT
                    replay_id,
                    {match_date_expr} AS match_date,
                    blue_team_name,
                    blue_goals,
                    orange_team_name,
                    orange_goals,
                    {_column_expr(remote_columns, "title")},
                    {_column_expr(remote_columns, "playlist_id", fallback_sql="'remote'")},
                    {_column_expr(remote_columns, "match_type")},
                    {_column_expr(remote_columns, "season_type")},
                    {_column_expr(remote_columns, "duration")},
                    {_column_expr(remote_columns, "overtime")},
                    {_column_expr(remote_columns, "group_names_json")}
                FROM remote_replays
                WHERE blue_team_name IS NOT NULL
                  AND orange_team_name IS NOT NULL
                  AND blue_goals IS NOT NULL
                  AND orange_goals IS NOT NULL
                """
                )
            )
        )
    if table_exists(con, "replay_parsed_status"):
        if table_exists(con, "remote_replays"):
            rows.extend(
                rows_to_dicts(
                    con.execute(
                    """
                    SELECT
                        ps.replay_id,
                        ps.parsed_at AS match_date,
                        ps.blue_team_name,
                        ps.blue_goals,
                        ps.orange_team_name,
                        ps.orange_goals,
                        NULL AS title,
                        'local' AS playlist_id,
                        NULL AS match_type,
                        NULL AS season_type,
                        ps.duration_seconds AS duration,
                        NULL AS overtime,
                        NULL AS group_names_json
                    FROM replay_parsed_status ps
                    LEFT JOIN remote_replays rr USING (replay_id)
                    WHERE ps.status = 'completed'
                      AND rr.replay_id IS NULL
                      AND ps.blue_team_name IS NOT NULL
                      AND ps.orange_team_name IS NOT NULL
                      AND ps.blue_goals IS NOT NULL
                      AND ps.orange_goals IS NOT NULL
                    """
                    )
                )
            )
        else:
            rows.extend(
                rows_to_dicts(
                    con.execute(
                    """
                    SELECT
                        replay_id,
                        parsed_at AS match_date,
                        blue_team_name,
                        blue_goals,
                        orange_team_name,
                        orange_goals,
                        NULL AS title,
                        'local' AS playlist_id,
                        NULL AS match_type,
                        NULL AS season_type,
                        duration_seconds AS duration,
                        NULL AS overtime,
                        NULL AS group_names_json
                    FROM replay_parsed_status
                    WHERE status = 'completed'
                      AND blue_team_name IS NOT NULL
                      AND orange_team_name IS NOT NULL
                      AND blue_goals IS NOT NULL
                      AND orange_goals IS NOT NULL
                    """
                    )
                )
            )
    telemetry = _telemetry_signal_map(con)
    filtered: list[dict[str, Any]] = []
    for row in rows:
        replay_id = row["replay_id"]
        blue_name = row["blue_team_name"]
        orange_name = row["orange_team_name"]
        if is_placeholder_team_name(blue_name) or is_placeholder_team_name(orange_name):
            continue
        blue_goals = int(row.get("blue_goals") or 0)
        orange_goals = int(row.get("orange_goals") or 0)
        total_goals = blue_goals + orange_goals
        goal_diff = abs(blue_goals - orange_goals)
        if max(blue_goals, orange_goals) > 9 or total_goals > 14 or goal_diff > 7:
            continue
        row["telemetry"] = telemetry.get(replay_id, {})
        filtered.append(row)

    def _sort_key(row: dict[str, Any]) -> tuple[datetime, str]:
        moment = _coerce_match_dt(row.get("match_date"))
        return (moment, row["replay_id"])

    filtered.sort(key=_sort_key)
    return filtered


def _leaderboard_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    return _safe_fetch(
        con,
        "live_leaderboards",
        """
        SELECT board_name, stage_key, region, rank, team_name, points
        FROM live_leaderboards
        ORDER BY points DESC, rank ASC
        """,
    )


def _coerce_match_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                return datetime.fromtimestamp(float(text), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def _match_power_context(row: dict[str, Any], *, latest_match_dt: datetime) -> dict[str, float]:
    title = row.get("title") or ""
    playlist_id = row.get("playlist_id") or ""
    match_type = row.get("match_type") or ""
    season_type = row.get("season_type") or ""
    group_names = " ".join(_parse_json(row.get("group_names_json"), []))
    haystack = f"{title} {playlist_id} {match_type} {season_type} {group_names}".lower()
    match_dt = _coerce_match_dt(row.get("match_date"))
    age_days = max(0.0, (latest_match_dt - match_dt).total_seconds() / 86400.0) if match_dt != datetime.min.replace(tzinfo=timezone.utc) else 365.0
    recency_weight = 0.68 + 0.52 * math.exp(-age_days / 120.0)

    tier_weight = 1.0
    if any(token in haystack for token in ("world championship", "worlds", "championship sunday")):
        tier_weight = 1.34
    elif "major" in haystack:
        tier_weight = 1.24
    elif "regional" in haystack:
        tier_weight = 1.14
    elif any(token in haystack for token in ("qualifier", "open ", "closed ", "invitational")):
        tier_weight = 1.07
    elif any(token in haystack for token in ("scrim", "showmatch", "ranked", "casual")):
        tier_weight = 0.9

    stage_weight = 1.0
    if any(token in haystack for token in ("grand final", "grand-final", "finals", "final")):
        stage_weight = 1.2
    elif any(token in haystack for token in ("semi", "semifinal")):
        stage_weight = 1.12
    elif any(token in haystack for token in ("quarter", "quarterfinal")):
        stage_weight = 1.08
    elif any(token in haystack for token in ("lower bracket", "upper bracket", "playoff")):
        stage_weight = 1.06
    elif any(token in haystack for token in ("swiss", "group stage", "round robin")):
        stage_weight = 1.02

    goal_diff = abs(int(row.get("blue_goals") or 0) - int(row.get("orange_goals") or 0))
    margin_weight = 1.0 + min(goal_diff, 4) * 0.15
    if row.get("overtime"):
        margin_weight = max(0.94, margin_weight - 0.08)

    telemetry_edge_blue = _telemetry_edge((row.get("telemetry") or {}).get("blue"), (row.get("telemetry") or {}).get("orange"))
    quality_weight = 1.0 + min(0.18, abs(telemetry_edge_blue) * 0.14)
    match_weight = recency_weight * tier_weight * stage_weight * margin_weight * quality_weight
    return {
        "recency_weight": recency_weight,
        "tier_weight": tier_weight,
        "stage_weight": stage_weight,
        "margin_weight": margin_weight,
        "quality_weight": quality_weight,
        "match_weight": match_weight,
        "telemetry_edge_blue": telemetry_edge_blue,
        "tier_bonus": ((tier_weight * stage_weight) - 1.0) * 24.0 * recency_weight,
    }


def _telemetry_signal_map(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, dict[str, float]]]:
    if not table_exists(con, "replay_parsed_events"):
        return {}
    rows = rows_to_dicts(
        con.execute(
            """
            SELECT
                replay_id,
                team_color,
                SUM(CASE WHEN event_type = 'goal' THEN 1 ELSE 0 END) AS goals,
                SUM(CASE WHEN event_type = 'touch' THEN 1 ELSE 0 END) AS touches,
                SUM(CASE WHEN event_type = 'demo' THEN 1 ELSE 0 END) AS demos,
                SUM(CASE WHEN event_type = 'pressure_phase' THEN 1 ELSE 0 END) AS pressure_phases,
                SUM(CASE WHEN event_type = 'turnover' THEN 1 ELSE 0 END) AS turnovers_forced,
                SUM(CASE WHEN event_type = 'kickoff_outcome' THEN 1 ELSE 0 END) AS kickoff_wins,
                SUM(CASE WHEN event_type = 'boost_starvation_window' THEN 1 ELSE 0 END) AS starvation_windows
            FROM replay_parsed_events
            WHERE team_color IN ('blue', 'orange')
            GROUP BY replay_id, team_color
            """
        )
    )
    turnover_committed_rows = rows_to_dicts(
        con.execute(
            """
            SELECT
                replay_id,
                other_team_color AS team_color,
                SUM(CASE WHEN event_type = 'turnover' THEN 1 ELSE 0 END) AS turnovers_committed
            FROM replay_parsed_events
            WHERE other_team_color IN ('blue', 'orange')
            GROUP BY replay_id, other_team_color
            """
        )
    )
    signals: dict[str, dict[str, dict[str, float]]] = {}
    for row in rows:
        replay = signals.setdefault(row["replay_id"], {"blue": {}, "orange": {}})
        replay[row["team_color"]] = {
            "goals": float(row.get("goals") or 0),
            "touches": float(row.get("touches") or 0),
            "demos": float(row.get("demos") or 0),
            "pressure_phases": float(row.get("pressure_phases") or 0),
            "turnovers_forced": float(row.get("turnovers_forced") or 0),
            "kickoff_wins": float(row.get("kickoff_wins") or 0),
            "starvation_windows": float(row.get("starvation_windows") or 0),
            "turnovers_committed": 0.0,
        }
    for row in turnover_committed_rows:
        replay = signals.setdefault(row["replay_id"], {"blue": {}, "orange": {}})
        team_bucket = replay.setdefault(row["team_color"], {})
        team_bucket["turnovers_committed"] = float(row.get("turnovers_committed") or 0)
    return signals


def _telemetry_edge(left: dict[str, Any] | None, right: dict[str, Any] | None) -> float:
    left = left or {}
    right = right or {}
    edge = 0.0
    edge += ((left.get("pressure_phases", 0.0) - right.get("pressure_phases", 0.0)) / 6.0) * 0.28
    edge += ((left.get("turnovers_forced", 0.0) - right.get("turnovers_forced", 0.0)) / 4.0) * 0.24
    edge -= ((left.get("turnovers_committed", 0.0) - right.get("turnovers_committed", 0.0)) / 4.0) * 0.24
    edge += ((left.get("kickoff_wins", 0.0) - right.get("kickoff_wins", 0.0)) / 3.0) * 0.11
    edge += ((left.get("demos", 0.0) - right.get("demos", 0.0)) / 3.0) * 0.08
    edge += ((left.get("starvation_windows", 0.0) - right.get("starvation_windows", 0.0)) / 3.0) * 0.07
    edge += ((left.get("touches", 0.0) - right.get("touches", 0.0)) / 40.0) * 0.05
    return max(-1.0, min(1.0, edge))


def _standings_stage_weight(row: dict[str, Any]) -> float:
    haystack = f"{row.get('board_name') or ''} {row.get('stage_key') or ''}".lower()
    if "world" in haystack:
        return 1.32
    if "major" in haystack:
        return 1.18
    if "regional" in haystack:
        return 1.08
    if any(token in haystack for token in ("qualifier", "open", "closed")):
        return 1.02
    return 1.0


def _standings_score(team: dict[str, Any]) -> float:
    points = int(team.get("standings_points") or 0)
    standings_rank = team.get("standings_rank")
    stage_weight = float(team.get("_standings_stage_weight") or 1.0)
    region_max_points = max(1, int(team.get("_standings_region_max_points") or points or 1))
    region_team_count = max(2, int(team.get("_standings_region_team_count") or 2))
    normalized_points = min(1.0, points / region_max_points)
    if standings_rank:
        rank_fraction = max(0.0, 1.0 - ((int(standings_rank) - 1) / max(1, region_team_count - 1)))
    else:
        rank_fraction = 0.0
    points_score = normalized_points * (58.0 * stage_weight)
    rank_score = rank_fraction * (26.0 * stage_weight)
    return round(points_score + rank_score, 3)


def _team_state(teams: dict[str, dict[str, Any]], team_name: str | None, *, resolver: IdentityResolver) -> dict[str, Any]:
    key = resolver.team_key(team_name)
    display_name = resolver.canonical_team_name(team_name)
    team = teams.setdefault(
        key,
        {
            "team_name": display_name or "Unknown",
            "elo": 1500.0,
            "rating": 1500.0,
            "power_score": 1500.0,
            "standings_score": 0.0,
            "schedule_score": 0.0,
            "form_score": 0.0,
            "dominance_score": 0.0,
            "quality_score": 0.0,
            "tier_score": 0.0,
            "standings_points": 0,
            "standings_rank": None,
            "standings_region": None,
            "wins": 0,
            "losses": 0,
            "games": 0,
            "win_rate": 0.5,
            "confidence": 0.0,
            "source_count": 0,
            "last_delta": 0.0,
            "goal_diff_total": 0.0,
            "weighted_games": 0.0,
            "schedule_strength_total": 0.0,
            "quality_total": 0.0,
            "tier_total": 0.0,
            "result_edge_total": 0.0,
            "delta_history": [],
            "avg_goal_diff": 0.0,
            "strength_of_schedule": 1500.0,
            "recent_form": 0.0,
            "last_match_date": None,
            "last_replay_id": None,
        },
    )
    candidate = resolver.canonical_team_name(team_name)
    if candidate and _better_display_name(candidate, team["team_name"]):
        team["team_name"] = candidate
    return team


def _team_key(team_name: str | None) -> str:
    return alias_key(team_name)


def _clean_display_name(team_name: str | None) -> str:
    value = clean_identity_text(team_name)
    return value or "Unknown"


def _better_display_name(candidate: str, current: str) -> bool:
    if not current:
        return True
    if current.isupper() and not candidate.isupper():
        return True
    if len(candidate) > len(current) and current.lower() in candidate.lower():
        return True
    return False


def _is_placeholder_team_name(team_name: str | None) -> bool:
    return is_placeholder_team_name(team_name)


def _apply_identity_display(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    resolver = IdentityResolver(con)
    for row in rows:
        if row.get("blue_team_name"):
            row["blue_team_name"] = resolver.canonical_team_name(row.get("blue_team_name"))
        if row.get("orange_team_name"):
            row["orange_team_name"] = resolver.canonical_team_name(row.get("orange_team_name"))
        if row.get("title") and row.get("blue_team_name") and row.get("orange_team_name"):
            row["title"] = f"{row['blue_team_name']} vs {row['orange_team_name']}"


def _canonicalize_boxscores(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows
    resolver = IdentityResolver(con)
    canonical: list[dict[str, Any]] = []
    for row in rows:
        player = resolver.resolve_player(row.get("platform_player_id"), row.get("player_name"), platform=row.get("platform"))
        item = dict(row)
        item["player_name"] = player["player_name"]
        if item.get("team_name"):
            item["team_name"] = resolver.canonical_team_name(item.get("team_name"))
        canonical.append(item)
    return canonical


def _canonicalize_top_players(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    resolver = IdentityResolver(con)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        player = resolver.resolve_player(row.get("platform_player_id"), row.get("player_name"), platform=row.get("platform"))
        bucket = grouped.setdefault(
            player["player_key"],
            {
                "player_name": player["player_name"],
                "platform": row.get("platform"),
                "platform_player_id": row.get("platform_player_id"),
                "replays": 0,
                "goals": 0,
                "assists": 0,
                "saves": 0,
                "avg_score_total": 0.0,
                "avg_score_weight": 0,
                "avg_boost_bpm_total": 0.0,
                "avg_boost_bpm_weight": 0,
            },
        )
        if _better_display_name(player["player_name"], bucket["player_name"]):
            bucket["player_name"] = player["player_name"]
        bucket["replays"] += int(row.get("replays") or 0)
        bucket["goals"] += int(row.get("goals") or 0)
        bucket["assists"] += int(row.get("assists") or 0)
        bucket["saves"] += int(row.get("saves") or 0)
        if row.get("avg_score") is not None:
            weight = max(1, int(row.get("replays") or 1))
            bucket["avg_score_total"] += float(row["avg_score"]) * weight
            bucket["avg_score_weight"] += weight
        if row.get("avg_boost_bpm") is not None:
            weight = max(1, int(row.get("replays") or 1))
            bucket["avg_boost_bpm_total"] += float(row["avg_boost_bpm"]) * weight
            bucket["avg_boost_bpm_weight"] += weight
    ordered = sorted(
        grouped.values(),
        key=lambda item: (item["goals"], item["assists"], item["replays"]),
        reverse=True,
    )[:limit]
    return [
        {
            "player_name": row["player_name"],
            "platform": row.get("platform"),
            "platform_player_id": row.get("platform_player_id"),
            "replays": row["replays"],
            "goals": row["goals"],
            "assists": row["assists"] or None,
            "saves": row["saves"] or None,
            "avg_score": round(row["avg_score_total"] / row["avg_score_weight"], 3) if row["avg_score_weight"] else None,
            "avg_boost_bpm": round(row["avg_boost_bpm_total"] / row["avg_boost_bpm_weight"], 3) if row["avg_boost_bpm_weight"] else None,
        }
        for row in ordered
    ]


def _table_stats(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    *,
    time_column: str,
    synced_column: str | None = None,
    filters: str = "",
) -> dict[str, Any]:
    if not table_exists(con, table_name):
        return {"count": 0, "max_time": None, "max_synced": None}
    columns = _table_columns(con, table_name)
    if time_column not in columns:
        return {"count": _count(con, table_name), "max_time": None, "max_synced": None}
    synced_expr = f"MAX({synced_column})" if synced_column and synced_column in columns else "NULL"
    stats = con.execute(
        f"""
        SELECT COUNT(*), MAX({time_column}), {synced_expr}
        FROM {table_name}
        {filters}
        """
    ).fetchone()
    return {
        "count": int(stats[0] or 0),
        "max_time": stats[1].isoformat() if stats and stats[1] else None,
        "max_synced": stats[2].isoformat() if stats and stats[2] else None,
    }
