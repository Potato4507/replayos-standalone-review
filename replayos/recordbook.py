from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import duckdb

from .db import rows_to_dicts
from .identity import IdentityResolver, alias_key, clean_identity_text, is_placeholder_team_name


_TOURNAMENT_SIGNAL_TOKENS = {
    "rlcs", "ewc", "gamers8", "fifae", "major", "regional", "world", "playoff", "playoffs",
    "grand", "final", "finals", "quarter", "semi", "swiss", "group", "open", "closed",
    "qualifier", "day", "bracket", "championship",
}
_NOISY_TEAM_PATTERN = re.compile(r"^(team ?\d+|\d+%?|\d+ ?x ?\d+)$", re.IGNORECASE)
_NOISY_PLAYER_PATTERN = re.compile(r"^(player ?\d+|\d+%?)$", re.IGNORECASE)


def _table_exists(con: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = con.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'main' AND table_name = ?
        LIMIT 1
        """,
        [table_name],
    ).fetchone()
    return bool(row)


def _fetch(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    return rows_to_dicts(con.execute(sql, params or []))


def _parse_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _tracker_profile_url(platform: str | None, platform_player_id: str | None, player_name: str | None) -> str | None:
    platform_key = (platform or "").strip().casefold()
    tracker_platform = {
        "steam": "steam",
        "epic": "epic",
        "xbox": "xbl",
        "xbl": "xbl",
        "xboxlive": "xbl",
        "psn": "psn",
        "playstation": "psn",
        "ps4": "psn",
        "ps5": "psn",
    }.get(platform_key)
    if tracker_platform and platform_player_id:
        return f"https://tracker.gg/rocket-league/profile/{tracker_platform}/{quote(platform_player_id)}/overview"
    candidate = player_name or platform_player_id
    if candidate:
        return f"https://tracker.gg/rocket-league/search?query={quote(candidate)}"
    return None


def _named_match_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    if not (_table_exists(con, "remote_replays") or _table_exists(con, "replay_parsed_status")):
        return []
    parts: list[str] = []
    if _table_exists(con, "remote_replays"):
        parts.append(
            """
            SELECT
                replay_id,
                COALESCE(match_date, downloaded_at, synced_at) AS match_date,
                blue_team_name,
                blue_goals,
                orange_team_name,
                orange_goals,
                title,
                season,
                season_type,
                playlist_id,
                group_names_json
            FROM remote_replays
            WHERE blue_team_name IS NOT NULL
              AND orange_team_name IS NOT NULL
              AND blue_goals IS NOT NULL
              AND orange_goals IS NOT NULL
            """
        )
    if _table_exists(con, "replay_parsed_status"):
        if _table_exists(con, "remote_replays"):
            parts.append(
                """
                SELECT
                    ps.replay_id,
                    ps.parsed_at AS match_date,
                    ps.blue_team_name,
                    ps.blue_goals,
                    ps.orange_team_name,
                    ps.orange_goals,
                    rr.title,
                    rr.season,
                    rr.season_type,
                    rr.playlist_id,
                    rr.group_names_json
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
        else:
            parts.append(
                """
                SELECT
                    replay_id,
                    parsed_at AS match_date,
                    blue_team_name,
                    blue_goals,
                    orange_team_name,
                    orange_goals,
                    NULL AS title,
                    NULL AS season,
                    NULL AS season_type,
                    NULL AS playlist_id,
                    NULL AS group_names_json
                FROM replay_parsed_status
                WHERE status = 'completed'
                  AND blue_team_name IS NOT NULL
                  AND orange_team_name IS NOT NULL
                  AND blue_goals IS NOT NULL
                  AND orange_goals IS NOT NULL
                """
            )
    rows = _fetch(con, f"{' UNION ALL '.join(parts)}")
    rows.sort(key=lambda row: ((row.get("match_date") or ""), row.get("replay_id") or ""))
    return rows


def _player_match_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if _table_exists(con, "replay_parsed_events") and _table_exists(con, "replay_parsed_status"):
        rows.extend(
            _fetch(
                con,
                """
                SELECT
                    pe.replay_id,
                    ps.parsed_at AS match_date,
                    pe.player_name,
                    ANY_VALUE(pe.player_id) AS player_id,
                    CAST(NULL AS VARCHAR) AS platform,
                    ANY_VALUE(pe.team_color) AS team_color,
                    CASE ANY_VALUE(pe.team_color)
                        WHEN 'blue' THEN ps.blue_team_name
                        ELSE ps.orange_team_name
                    END AS team_name,
                    SUM(CASE WHEN pe.event_type = 'goal' THEN 1 ELSE 0 END) AS goals,
                    SUM(CASE WHEN pe.event_type = 'touch' THEN 1 ELSE 0 END) AS touches,
                    SUM(CASE WHEN pe.event_type = 'demo' THEN 1 ELSE 0 END) AS demos,
                    SUM(CASE WHEN pe.event_type = 'pressure_phase' THEN 1 ELSE 0 END) AS pressure_phases,
                    SUM(CASE WHEN pe.event_type = 'turnover' THEN 1 ELSE 0 END) AS turnovers_forced,
                    SUM(CASE WHEN pe.event_type = 'kickoff_outcome' THEN 1 ELSE 0 END) AS kickoff_wins,
                    SUM(CASE WHEN pe.event_type = 'boost_starvation_window' THEN 1 ELSE 0 END) AS starvation_windows,
                    CAST(NULL AS BIGINT) AS shots,
                    CAST(NULL AS BIGINT) AS saves,
                    SUM(
                        CASE
                            WHEN pe.event_type = 'goal' THEN 100
                            WHEN pe.event_type = 'touch' THEN 4
                            WHEN pe.event_type = 'demo' THEN 15
                            WHEN pe.event_type = 'pressure_phase' THEN 10
                            ELSE 0
                        END
                    ) AS score
                FROM replay_parsed_events pe
                JOIN replay_parsed_status ps USING (replay_id)
                WHERE ps.status = 'completed'
                  AND pe.player_name IS NOT NULL
                GROUP BY pe.replay_id, ps.parsed_at, ps.blue_team_name, ps.orange_team_name, pe.player_name
                """
            )
        )
    if _table_exists(con, "remote_players") and _table_exists(con, "remote_replays"):
        rows.extend(
            _fetch(
                con,
                """
                SELECT
                    rp.replay_id,
                    COALESCE(rr.match_date, rr.downloaded_at, rr.synced_at) AS match_date,
                    rp.player_name,
                    COALESCE(rp.platform_player_id, rp.player_name) AS player_id,
                    rp.platform,
                    rp.side AS team_color,
                    CASE rp.side
                        WHEN 'blue' THEN rr.blue_team_name
                        ELSE rr.orange_team_name
                    END AS team_name,
                    COALESCE(rp.goals, 0) AS goals,
                    CAST(NULL AS BIGINT) AS touches,
                    COALESCE(rp.demos_inflicted, 0) AS demos,
                    CAST(NULL AS BIGINT) AS pressure_phases,
                    CAST(NULL AS BIGINT) AS turnovers_forced,
                    CAST(NULL AS BIGINT) AS kickoff_wins,
                    CAST(NULL AS BIGINT) AS starvation_windows,
                    COALESCE(rp.shots, 0) AS shots,
                    COALESCE(rp.saves, 0) AS saves,
                    COALESCE(rp.score, 0) AS score
                FROM remote_players rp
                JOIN remote_replays rr USING (replay_id)
                WHERE rp.player_name IS NOT NULL
                """
            )
        )
    rows.sort(key=lambda row: ((row.get("match_date") or ""), row.get("replay_id") or "", row.get("player_name") or ""))
    return rows


def _parsed_event_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    if not _table_exists(con, "replay_parsed_events"):
        return []
    return _fetch(
        con,
        """
        SELECT
            replay_id,
            event_id,
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
        ORDER BY replay_id, t, event_id
        """
    )


def _canonical_matches(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    resolver = IdentityResolver(con)
    rows: list[dict[str, Any]] = []
    for row in _named_match_rows(con):
        blue_name = resolver.canonical_team_name(row.get("blue_team_name"))
        orange_name = resolver.canonical_team_name(row.get("orange_team_name"))
        if is_placeholder_team_name(blue_name) or is_placeholder_team_name(orange_name):
            continue
        rows.append(
            {
                **row,
                "blue_team_name": blue_name,
                "orange_team_name": orange_name,
                "tournament_name": _display_tournament_name(row),
                "is_rlcs_context": _is_rlcs_context(row),
            }
        )
    return rows


def _canonical_player_matches(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    resolver = IdentityResolver(con)
    matches_by_replay = {row["replay_id"]: row for row in _canonical_matches(con)}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in _player_match_rows(con):
        match = matches_by_replay.get(row.get("replay_id"))
        if not match:
            continue
        resolved = resolver.resolve_player(row.get("player_id"), row.get("player_name"), platform=row.get("platform"))
        team_color = row.get("team_color") or "blue"
        if team_color == "blue":
            team_name = match["blue_team_name"]
        elif team_color == "orange":
            team_name = match["orange_team_name"]
        else:
            team_name = resolver.canonical_team_name(row.get("team_name"))
        dedupe_key = (row.get("replay_id") or "", resolved["player_key"], team_color)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rows.append(
            {
                **row,
                "player_key": resolved["player_key"],
                "player_name": resolved["player_name"],
                "team_name": team_name,
                "blue_team_name": match["blue_team_name"],
                "orange_team_name": match["orange_team_name"],
                "blue_goals": int(match["blue_goals"] or 0),
                "orange_goals": int(match["orange_goals"] or 0),
                "match_date": row.get("match_date") or match.get("match_date"),
                "tournament_name": match.get("tournament_name"),
                "is_rlcs_context": bool(match.get("is_rlcs_context")),
            }
        )
    rows.sort(key=lambda row: ((row.get("match_date") or ""), row.get("replay_id") or "", row.get("player_name") or ""))
    return rows


def _canonical_parsed_events(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    resolver = IdentityResolver(con)
    matches_by_replay = {row["replay_id"]: row for row in _canonical_matches(con)}
    events: list[dict[str, Any]] = []
    for row in _parsed_event_rows(con):
        match = matches_by_replay.get(row.get("replay_id"))
        if not match:
            continue
        event = dict(row)
        if event.get("player_name") or event.get("player_id"):
            resolved = resolver.resolve_player(event.get("player_id"), event.get("player_name"))
            event["player_key"] = resolved["player_key"]
            event["player_name"] = resolved["player_name"]
        if event.get("other_player_name") or event.get("other_player_id"):
            resolved_other = resolver.resolve_player(event.get("other_player_id"), event.get("other_player_name"))
            event["other_player_key"] = resolved_other["player_key"]
            event["other_player_name"] = resolved_other["player_name"]
        if event.get("team_color") == "blue":
            event["team_name"] = match["blue_team_name"]
        elif event.get("team_color") == "orange":
            event["team_name"] = match["orange_team_name"]
        else:
            event["team_name"] = None
        if event.get("other_team_color") == "blue":
            event["other_team_name"] = match["blue_team_name"]
        elif event.get("other_team_color") == "orange":
            event["other_team_name"] = match["orange_team_name"]
        else:
            event["other_team_name"] = None
        events.append(event)
    return events


def _resolve_name(options: list[str], candidate: str) -> str | None:
    normalized = candidate.strip().lower()
    for option in options:
        if option.lower() == normalized:
            return option
    return None


def _better_name(candidate: str | None, current: str | None) -> bool:
    if not candidate:
        return False
    if not current:
        return True
    if current.isupper() and not candidate.isupper():
        return True
    if len(candidate) > len(current) and current.casefold() in candidate.casefold():
        return True
    return False


def _has_enough_letters(value: str, minimum: int = 2) -> bool:
    return sum(1 for char in value if char.isalpha()) >= minimum


def _is_viable_team_name(name: str | None) -> bool:
    cleaned = clean_identity_text(name)
    if not cleaned or cleaned == "Unknown" or is_placeholder_team_name(cleaned):
        return False
    normalized = alias_key(cleaned)
    if not normalized or len(normalized) <= 1:
        return False
    if _NOISY_TEAM_PATTERN.fullmatch(normalized):
        return False
    if "%" in cleaned and not _has_enough_letters(cleaned, 3):
        return False
    if not _has_enough_letters(cleaned, 2):
        return False
    return True


def _is_viable_player_name(name: str | None) -> bool:
    cleaned = clean_identity_text(name)
    if not cleaned or cleaned == "Unknown":
        return False
    normalized = alias_key(cleaned)
    if not normalized or len(normalized) <= 1:
        return False
    if _NOISY_PLAYER_PATTERN.fullmatch(normalized):
        return False
    if not _has_enough_letters(cleaned, 2):
        return False
    return True


def _dedupe_display_names(names: list[str]) -> list[str]:
    chosen: dict[str, str] = {}
    for name in names:
        cleaned = clean_identity_text(name)
        key = alias_key(cleaned)
        if not key:
            continue
        current = chosen.get(key)
        if current is None or _better_name(cleaned, current):
            chosen[key] = cleaned
    return sorted(chosen.values(), key=str.casefold)


def _tournament_name_quality(name: str | None) -> float:
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
    if any(token in _TOURNAMENT_SIGNAL_TOKENS for token in tokens):
        score += 2.6
    if "vs" in tokens:
        score += 0.8
    if any(char.isdigit() for char in cleaned):
        score += 0.2
    if re.fullmatch(r"[a-z]{7,}", compact) and compact not in _TOURNAMENT_SIGNAL_TOKENS:
        score -= 4.0
    if compact in {"nghjghkgm", "fdgdfgdfgdf"}:
        score -= 6.0
    return score


def _display_tournament_name(row: dict[str, Any]) -> str | None:
    group_names = _parse_json(row.get("group_names_json"), [])
    candidates = [clean_identity_text(value) for value in group_names if clean_identity_text(value)]
    title = clean_identity_text(row.get("title"))
    season = row.get("season")
    season_type = clean_identity_text(row.get("season_type"))
    if season:
        if any("ewc" in alias_key(value) for value in candidates + ([title] if title else [])):
            candidates.append(f"EWC {int(season)}")
        if any("rlcs" in alias_key(value) for value in candidates + ([title] if title else [])):
            candidates.append(f"RLCS {int(season)}")
    if season_type:
        candidates.append(season_type)
    if title:
        candidates.append(title)
    best = None
    best_score = 0.0
    for candidate in candidates:
        score = _tournament_name_quality(candidate)
        if score > best_score:
            best = candidate
            best_score = score
    return best if best_score >= 1.5 else None


def _is_rlcs_context(row: dict[str, Any]) -> bool:
    haystack = " ".join(
        [
            clean_identity_text(row.get("title")),
            clean_identity_text(row.get("season_type")),
            " ".join(_parse_json(row.get("group_names_json"), [])),
        ]
    ).casefold()
    return any(token in haystack for token in ("rlcs", "major", "regional", "qualifier", "world championship", "worlds", "ewc"))


def _minimum_games(rows: list[dict[str, Any]], preferred: int) -> int:
    return preferred if any(int(row.get("games") or 0) >= preferred for row in rows) else 1


def _top_rows(
    rows: list[dict[str, Any]],
    *,
    label_key: str,
    value_key: str,
    limit: int,
    sort_keys: list[str],
    minimum_games: int = 1,
) -> list[dict[str, Any]]:
    filtered = [row for row in rows if int(row.get("games") or 0) >= minimum_games]
    ordered = sorted(
        filtered,
        key=lambda row: tuple(row.get(key) or 0 for key in sort_keys),
        reverse=True,
    )[:limit]
    return [
        {
            "name": row.get(label_key),
            "value": row.get(value_key),
            "games": row.get("games"),
            "wins": row.get("wins"),
            "losses": row.get("losses"),
            "confidence": round(min(1.0, (int(row.get("games") or 0) / max(minimum_games, 1))) if minimum_games else 1.0, 3),
            "tournament_name": row.get("tournament_name"),
        }
        for row in ordered
    ]


def _rate_rows(
    rows: list[dict[str, Any]],
    *,
    numerator_key: str,
    denominator_key: str,
    output_key: str,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        denominator = int(row.get(denominator_key) or 0)
        enriched.append(
            {
                **row,
                "games": denominator,
                output_key: round((float(row.get(numerator_key) or 0) / denominator) if denominator else 0.0, 4),
            }
        )
    return enriched


def _known_team_names(con: duckdb.DuckDBPyConnection) -> list[str]:
    resolver = IdentityResolver(con)
    teams: set[str] = set()
    queries = [
        "SELECT team_name FROM live_leaderboards WHERE team_name IS NOT NULL",
        "SELECT team_a AS team_name FROM live_matches WHERE team_a IS NOT NULL",
        "SELECT team_b AS team_name FROM live_matches WHERE team_b IS NOT NULL",
        "SELECT blue_team_name AS team_name FROM remote_replays WHERE blue_team_name IS NOT NULL",
        "SELECT orange_team_name AS team_name FROM remote_replays WHERE orange_team_name IS NOT NULL",
        "SELECT blue_team_name AS team_name FROM replay_parsed_status WHERE status = 'completed' AND blue_team_name IS NOT NULL",
        "SELECT orange_team_name AS team_name FROM replay_parsed_status WHERE status = 'completed' AND orange_team_name IS NOT NULL",
    ]
    for query in queries:
        try:
            rows = con.execute(query).fetchall()
        except duckdb.Error:
            continue
        for (team_name,) in rows:
            canonical = resolver.canonical_team_name(team_name)
            if _is_viable_team_name(canonical):
                teams.add(canonical)
    return sorted(teams, key=str.casefold)


def _known_player_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    resolver = IdentityResolver(con)
    grouped: dict[str, dict[str, Any]] = {}
    queries = [
        """
        SELECT DISTINCT player_name, player_id, CAST(NULL AS VARCHAR) AS platform, CAST(NULL AS VARCHAR) AS platform_player_id
        FROM replay_parsed_events
        WHERE player_name IS NOT NULL
        """,
        """
        SELECT DISTINCT player_name, platform_player_id AS player_id, platform, platform_player_id
        FROM remote_players
        WHERE player_name IS NOT NULL
        """,
    ]
    for query in queries:
        try:
            rows = rows_to_dicts(con.execute(query))
        except duckdb.Error:
            continue
        for row in rows:
            resolved = resolver.resolve_player(row.get("player_id"), row.get("player_name"), platform=row.get("platform"))
            if not _is_viable_player_name(resolved["player_name"]):
                continue
            bucket = grouped.setdefault(
                resolved["player_key"],
                {
                    "player_key": resolved["player_key"],
                    "player_name": resolved["player_name"],
                    "platform": row.get("platform"),
                    "platform_player_id": row.get("platform_player_id") or row.get("player_id"),
                },
            )
            if _better_name(resolved["player_name"], bucket.get("player_name")):
                bucket["player_name"] = resolved["player_name"]
            if not bucket.get("platform") and row.get("platform"):
                bucket["platform"] = row.get("platform")
            if not bucket.get("platform_player_id") and (row.get("platform_player_id") or row.get("player_id")):
                bucket["platform_player_id"] = row.get("platform_player_id") or row.get("player_id")
    if _table_exists(con, "live_leaderboards"):
        try:
            leaderboard_rows = rows_to_dicts(con.execute("SELECT players_json FROM live_leaderboards WHERE players_json IS NOT NULL"))
        except duckdb.Error:
            leaderboard_rows = []
        for row in leaderboard_rows:
            for player_name in _parse_json(row.get("players_json"), []):
                resolved = resolver.resolve_player(None, player_name)
                if not _is_viable_player_name(resolved["player_name"]):
                    continue
                grouped.setdefault(
                    resolved["player_key"],
                    {
                        "player_key": resolved["player_key"],
                        "player_name": resolved["player_name"],
                        "platform": None,
                        "platform_player_id": None,
                    },
                )
    return sorted(grouped.values(), key=lambda row: row["player_name"].casefold())


def _team_live_snapshot(con: duckdb.DuckDBPyConnection, team_name: str) -> list[dict[str, Any]]:
    resolver = IdentityResolver(con)
    if not _table_exists(con, "live_leaderboards"):
        return []
    rows = _fetch(
        con,
        """
        SELECT board_name, stage_key, region, rank, team_name, points, updated_at
        FROM live_leaderboards
        WHERE team_name IS NOT NULL
        ORDER BY points DESC, rank ASC
        """,
    )
    snapshots: list[dict[str, Any]] = []
    for row in rows:
        if resolver.canonical_team_name(row.get("team_name")) != team_name:
            continue
        snapshots.append(
            {
                "board_name": row.get("board_name"),
                "stage_key": row.get("stage_key"),
                "region": row.get("region"),
                "rank": int(row.get("rank") or 0),
                "points": int(row.get("points") or 0),
                "updated_at": row.get("updated_at").isoformat() if hasattr(row.get("updated_at"), "isoformat") else row.get("updated_at"),
            }
        )
    return snapshots[:6]


def _team_summary_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    teams: dict[str, dict[str, Any]] = {}
    resolver = IdentityResolver(con)
    for row in _canonical_matches(con):
        for team_name, opponent_name, goals_for, goals_against in (
            (row["blue_team_name"], row["orange_team_name"], int(row["blue_goals"] or 0), int(row["orange_goals"] or 0)),
            (row["orange_team_name"], row["blue_team_name"], int(row["orange_goals"] or 0), int(row["blue_goals"] or 0)),
        ):
            key = resolver.team_key(team_name)
            bucket = teams.setdefault(
                key,
                {
                    "team_name": team_name,
                    "games": 0,
                    "wins": 0,
                    "losses": 0,
                    "goals_for": 0,
                    "goals_against": 0,
                    "goal_diff": 0,
                    "goals_per_game": 0.0,
                    "shutouts": 0,
                    "rlcs_games": 0,
                    "rlcs_wins": 0,
                    "rlcs_goals_for": 0,
                    "last_played": None,
                },
            )
            bucket["games"] += 1
            bucket["wins"] += int(goals_for > goals_against)
            bucket["losses"] += int(goals_for < goals_against)
            bucket["goals_for"] += goals_for
            bucket["goals_against"] += goals_against
            bucket["goal_diff"] = bucket["goals_for"] - bucket["goals_against"]
            bucket["shutouts"] += int(goals_against == 0)
            if row.get("is_rlcs_context"):
                bucket["rlcs_games"] += 1
                bucket["rlcs_wins"] += int(goals_for > goals_against)
                bucket["rlcs_goals_for"] += goals_for
            if row.get("match_date") and (bucket["last_played"] is None or row["match_date"] > bucket["last_played"]):
                bucket["last_played"] = row["match_date"]
    rows = list(teams.values())
    for row in rows:
        games = int(row["games"] or 0)
        row["goals_per_game"] = round((row["goals_for"] / games) if games else 0.0, 3)
    rows.sort(key=lambda row: (row["wins"], row["goal_diff"], row["goals_for"], row["team_name"]), reverse=True)
    return rows


def _player_summary_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    players: dict[str, dict[str, Any]] = {}
    for row in _canonical_player_matches(con):
        bucket = players.setdefault(
            row["player_key"],
            {
                "player_key": row["player_key"],
                "player_name": row["player_name"],
                "platform": row.get("platform"),
                "platform_player_id": row.get("player_id"),
                "games": 0,
                "wins": 0,
                "losses": 0,
                "goals": 0,
                "touches": 0,
                "demos": 0,
                "pressure_phases": 0,
                "turnovers_forced": 0,
                "kickoff_wins": 0,
                "starvation_windows": 0,
                "shots": 0,
                "saves": 0,
                "rlcs_games": 0,
                "rlcs_wins": 0,
                "rlcs_goals": 0,
                "avg_score": 0.0,
                "score_total": 0.0,
                "last_played": None,
            },
        )
        if row["team_color"] == "blue":
            goals_for = int(row["blue_goals"])
            goals_against = int(row["orange_goals"])
        else:
            goals_for = int(row["orange_goals"])
            goals_against = int(row["blue_goals"])
        bucket["games"] += 1
        bucket["wins"] += int(goals_for > goals_against)
        bucket["losses"] += int(goals_for < goals_against)
        bucket["goals"] += int(row.get("goals") or 0)
        bucket["touches"] += int(row.get("touches") or 0)
        bucket["demos"] += int(row.get("demos") or 0)
        bucket["pressure_phases"] += int(row.get("pressure_phases") or 0)
        bucket["turnovers_forced"] += int(row.get("turnovers_forced") or 0)
        bucket["kickoff_wins"] += int(row.get("kickoff_wins") or 0)
        bucket["starvation_windows"] += int(row.get("starvation_windows") or 0)
        bucket["shots"] += int(row.get("shots") or 0)
        bucket["saves"] += int(row.get("saves") or 0)
        if row.get("is_rlcs_context"):
            bucket["rlcs_games"] += 1
            bucket["rlcs_wins"] += int(goals_for > goals_against)
            bucket["rlcs_goals"] += int(row.get("goals") or 0)
        bucket["score_total"] += float(row.get("score") or 0.0)
        if not bucket.get("platform") and row.get("platform"):
            bucket["platform"] = row.get("platform")
        if not bucket.get("platform_player_id") and row.get("player_id"):
            bucket["platform_player_id"] = row.get("player_id")
        if row.get("match_date") and (bucket["last_played"] is None or row["match_date"] > bucket["last_played"]):
            bucket["last_played"] = row["match_date"]
    rows = list(players.values())
    for row in rows:
        games = int(row["games"] or 0)
        row["avg_score"] = round((row["score_total"] / games) if games else 0.0, 3)
        row["goals_per_game"] = round((int(row["goals"]) / games) if games else 0.0, 3)
    rows.sort(key=lambda row: (row["wins"], row["goals"], row["touches"], row["player_name"]), reverse=True)
    return rows


def _roster_summary_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rosters: dict[str, dict[str, Any]] = {}
    matches_by_replay = {row["replay_id"]: row for row in _canonical_matches(con)}
    rows_by_replay: dict[str, list[dict[str, Any]]] = {}
    for row in _canonical_player_matches(con):
        rows_by_replay.setdefault(row["replay_id"], []).append(row)
    for replay_rows in rows_by_replay.values():
        sides: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in replay_rows:
            sides.setdefault((row["replay_id"], row["team_color"]), []).append(row)
        for side_rows in sides.values():
            if not side_rows:
                continue
            team_name = side_rows[0]["team_name"]
            team_color = side_rows[0]["team_color"]
            match_date = side_rows[0].get("match_date")
            if team_color == "blue":
                goals_for = int(side_rows[0]["blue_goals"])
                goals_against = int(side_rows[0]["orange_goals"])
            else:
                goals_for = int(side_rows[0]["orange_goals"])
                goals_against = int(side_rows[0]["blue_goals"])
            lineup = sorted({row["player_name"] for row in side_rows if row.get("player_name")}, key=str.casefold)
            roster_display = " / ".join(lineup) if lineup else team_name
            roster_key = f"{team_name}::{ '|'.join(lineup) }"
            bucket = rosters.setdefault(
                roster_key,
                {
                    "team_name": team_name,
                    "roster_name": roster_display,
                    "players": lineup,
                    "games": 0,
                    "wins": 0,
                    "losses": 0,
                    "goals_for": 0,
                    "goals_against": 0,
                    "goal_diff": 0,
                    "last_played": None,
                    "tournament_name": None,
                    "rlcs_games": 0,
                    "rlcs_wins": 0,
                },
            )
            bucket["games"] += 1
            bucket["wins"] += int(goals_for > goals_against)
            bucket["losses"] += int(goals_for < goals_against)
            bucket["goals_for"] += goals_for
            bucket["goals_against"] += goals_against
            bucket["goal_diff"] = bucket["goals_for"] - bucket["goals_against"]
            if match_date and (bucket["last_played"] is None or match_date > bucket["last_played"]):
                bucket["last_played"] = match_date
            match_meta = matches_by_replay.get(side_rows[0]["replay_id"])
            if match_meta:
                tournament_name = match_meta.get("tournament_name")
                if tournament_name and not bucket.get("tournament_name"):
                    bucket["tournament_name"] = tournament_name
                if match_meta.get("is_rlcs_context"):
                    bucket["rlcs_games"] += 1
                    bucket["rlcs_wins"] += int(goals_for > goals_against)
    rows = list(rosters.values())
    for row in rows:
        games = int(row["games"] or 0)
        row["win_rate"] = round((int(row["wins"]) / games) if games else 0.0, 4)
    rows.sort(key=lambda row: (row["wins"], row["win_rate"], row["goal_diff"], row["roster_name"]), reverse=True)
    return rows


def _event_team_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    event_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _canonical_matches(con):
        tournament_name = row.get("tournament_name")
        if not tournament_name:
            continue
        for team_name, opponent_name, goals_for, goals_against in (
            (row["blue_team_name"], row["orange_team_name"], int(row["blue_goals"] or 0), int(row["orange_goals"] or 0)),
            (row["orange_team_name"], row["blue_team_name"], int(row["orange_goals"] or 0), int(row["blue_goals"] or 0)),
        ):
            key = (tournament_name, team_name)
            bucket = event_rows.setdefault(
                key,
                {
                    "tournament_name": tournament_name,
                    "team_name": team_name,
                    "is_rlcs_context": bool(row.get("is_rlcs_context")),
                    "games": 0,
                    "wins": 0,
                    "losses": 0,
                    "goals_for": 0,
                    "goals_against": 0,
                    "goal_diff": 0,
                    "last_played": None,
                },
            )
            bucket["games"] += 1
            bucket["wins"] += int(goals_for > goals_against)
            bucket["losses"] += int(goals_for < goals_against)
            bucket["goals_for"] += goals_for
            bucket["goals_against"] += goals_against
            bucket["goal_diff"] = bucket["goals_for"] - bucket["goals_against"]
            if row.get("match_date") and (bucket["last_played"] is None or row["match_date"] > bucket["last_played"]):
                bucket["last_played"] = row["match_date"]
    rows = list(event_rows.values())
    rows.sort(key=lambda row: (row["wins"], row["games"], row["goal_diff"], row["team_name"]), reverse=True)
    return rows


def _team_matchup_rows(con: duckdb.DuckDBPyConnection, *, limit: int = 10) -> list[dict[str, Any]]:
    pairings: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _canonical_matches(con):
        team_a, team_b = sorted([row["blue_team_name"], row["orange_team_name"]], key=str.casefold)
        key = (team_a, team_b)
        bucket = pairings.setdefault(key, {"team_a": team_a, "team_b": team_b, "games": 0, "last_played": None})
        bucket["games"] += 1
        if row.get("match_date") and (bucket["last_played"] is None or row["match_date"] > bucket["last_played"]):
            bucket["last_played"] = row["match_date"]
    rows = list(pairings.values())
    rows.sort(key=lambda row: (row["games"], row["last_played"] or "", row["team_a"], row["team_b"]), reverse=True)
    return rows[:limit]


def _player_matchup_rows(con: duckdb.DuckDBPyConnection, *, limit: int = 10) -> list[dict[str, Any]]:
    rows_by_replay: dict[str, list[dict[str, Any]]] = {}
    for row in _canonical_player_matches(con):
        rows_by_replay.setdefault(row["replay_id"], []).append(row)
    pairings: dict[tuple[str, str], dict[str, Any]] = {}
    for replay_rows in rows_by_replay.values():
        unique_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for row in replay_rows:
            unique_rows[(row["player_key"], row["team_color"])] = row
        values = list(unique_rows.values())
        for left in values:
            for right in values:
                if left["player_key"] >= right["player_key"]:
                    continue
                if left["team_color"] == right["team_color"]:
                    continue
                key = (left["player_name"], right["player_name"])
                bucket = pairings.setdefault(key, {"player_a": left["player_name"], "player_b": right["player_name"], "games": 0})
                bucket["games"] += 1
    rows = list(pairings.values())
    rows.sort(key=lambda row: (row["games"], row["player_a"], row["player_b"]), reverse=True)
    return rows[:limit]


def _team_streaks(match_rows: list[dict[str, Any]]) -> dict[str, Any]:
    longest_win = 0
    longest_loss = 0
    current_type = None
    current_run = 0
    win_run = 0
    loss_run = 0
    for row in match_rows:
        result = "win" if int(row["goals_for"]) > int(row["goals_against"]) else "loss" if int(row["goals_for"]) < int(row["goals_against"]) else "draw"
        if result == "win":
            win_run += 1
            loss_run = 0
            longest_win = max(longest_win, win_run)
        elif result == "loss":
            loss_run += 1
            win_run = 0
            longest_loss = max(longest_loss, loss_run)
        else:
            win_run = 0
            loss_run = 0
        if result == current_type:
            current_run += 1
        else:
            current_type = result
            current_run = 1
    return {
        "longest_win_streak": longest_win,
        "longest_loss_streak": longest_loss,
        "current_streak_type": current_type,
        "current_streak": current_run if current_type else 0,
    }


def _frequency_items(row: dict[str, Any], *, games_key: str = "telemetry_games") -> list[dict[str, Any]]:
    games = int(row.get(games_key) or 0)
    keys: list[tuple[str, str]] = [
        ("goals", "Goals"),
        ("touches", "Touches"),
        ("demos", "Demos"),
        ("kickoff_wins", "Kickoff wins"),
        ("turnovers_forced", "Turnovers forced"),
        ("turnovers_committed", "Turnovers committed"),
    ]
    if int(row.get("shots") or 0):
        keys.append(("shots", "Shots"))
    if int(row.get("saves") or 0):
        keys.append(("saves", "Saves"))
    if int(row.get("starvation_windows") or 0):
        keys.append(("starvation_windows", "Starvation windows"))
    items = []
    for key, label in keys:
        total = int(row.get(key) or 0)
        items.append(
            {
                "event_type": key,
                "label": label,
                "total": total,
                "per_game": round(total / games, 3) if games else 0.0,
            }
        )
    return items


def recordbook_overview(con: duckdb.DuckDBPyConnection, *, limit: int = 8) -> dict[str, Any]:
    team_rows = _team_summary_rows(con)
    player_rows = _player_summary_rows(con)
    roster_rows = _roster_summary_rows(con)
    event_rows = _event_team_rows(con)
    team_options = _known_team_names(con)
    player_known_rows = _known_player_rows(con)
    player_options = _dedupe_display_names([row["player_name"] for row in player_known_rows if row.get("player_name")])
    team_min_games = _minimum_games(team_rows, 5)
    player_min_games = _minimum_games(player_rows, 5)
    roster_min_games = _minimum_games(roster_rows, 3)
    team_activity_min_games = _minimum_games(team_rows, 4)
    player_activity_min_games = _minimum_games(player_rows, 4)
    rlcs_team_rows = [{**row, "games": int(row.get("rlcs_games") or 0)} for row in team_rows if int(row.get("rlcs_games") or 0) > 0]
    rlcs_player_rows = [{**row, "games": int(row.get("rlcs_games") or 0)} for row in player_rows if int(row.get("rlcs_games") or 0) > 0]
    rlcs_roster_rows = [{**row, "games": int(row.get("rlcs_games") or 0)} for row in roster_rows if int(row.get("rlcs_games") or 0) > 0]
    rlcs_event_rows = [row for row in event_rows if row.get("is_rlcs_context")]
    team_with_win_rate = _rate_rows(team_rows, numerator_key="wins", denominator_key="games", output_key="win_rate")
    player_with_win_rate = _rate_rows(player_rows, numerator_key="wins", denominator_key="games", output_key="win_rate")
    roster_with_win_rate = _rate_rows(roster_rows, numerator_key="wins", denominator_key="games", output_key="win_rate")
    rlcs_team_with_win_rate = _rate_rows(team_rows, numerator_key="rlcs_wins", denominator_key="rlcs_games", output_key="win_rate")
    rlcs_roster_with_win_rate = _rate_rows(roster_rows, numerator_key="rlcs_wins", denominator_key="rlcs_games", output_key="win_rate")
    rlcs_team_scoring_rows = _rate_rows(team_rows, numerator_key="rlcs_goals_for", denominator_key="rlcs_games", output_key="goals_per_game")
    rlcs_player_goal_rate_rows = _rate_rows(player_rows, numerator_key="rlcs_goals", denominator_key="rlcs_games", output_key="goals_per_game")
    rlcs_event_with_win_rate = _rate_rows(rlcs_event_rows, numerator_key="wins", denominator_key="games", output_key="win_rate")
    tracked_matches = sum(int(row.get("games") or 0) for row in team_rows) // 2
    return {
        "summary": {
            "tracked_matches": tracked_matches,
            "tracked_teams": len(team_rows),
            "tracked_players": len(player_rows),
            "known_teams": len(team_options),
            "known_players": len(player_options),
            "minimums": {
                "team": team_min_games,
                "player": player_min_games,
                "roster": roster_min_games,
            },
        },
        "team_options": team_options,
        "player_options": player_options,
        "team_leaders": {
            "most_wins": _top_rows(team_rows, label_key="team_name", value_key="wins", limit=limit, sort_keys=["wins", "goal_diff", "goals_for"], minimum_games=team_activity_min_games),
            "best_win_rate": _top_rows(
                team_with_win_rate,
                label_key="team_name",
                value_key="win_rate",
                limit=limit,
                sort_keys=["win_rate", "wins", "goal_diff"],
                minimum_games=team_min_games,
            ),
            "best_scoring_rate": _top_rows(team_rows, label_key="team_name", value_key="goals_per_game", limit=limit, sort_keys=["goals_per_game", "goals_for", "wins"], minimum_games=team_min_games),
            "best_goal_diff": _top_rows(team_rows, label_key="team_name", value_key="goal_diff", limit=limit, sort_keys=["goal_diff", "wins", "goals_for"], minimum_games=team_min_games),
            "rlcs_most_wins": _top_rows(rlcs_team_rows, label_key="team_name", value_key="rlcs_wins", limit=limit, sort_keys=["rlcs_wins", "wins", "goal_diff"], minimum_games=2),
            "rlcs_best_win_rate": _top_rows(rlcs_team_with_win_rate, label_key="team_name", value_key="win_rate", limit=limit, sort_keys=["win_rate", "rlcs_wins", "goal_diff"], minimum_games=2),
            "rlcs_best_scoring_rate": _top_rows(rlcs_team_scoring_rows, label_key="team_name", value_key="goals_per_game", limit=limit, sort_keys=["goals_per_game", "rlcs_wins", "wins"], minimum_games=2),
        },
        "player_leaders": {
            "most_wins": _top_rows(player_rows, label_key="player_name", value_key="wins", limit=limit, sort_keys=["wins", "goals", "touches"], minimum_games=player_activity_min_games),
            "most_goals": _top_rows(player_rows, label_key="player_name", value_key="goals", limit=limit, sort_keys=["goals", "wins", "touches"], minimum_games=player_activity_min_games),
            "most_touches": _top_rows(player_rows, label_key="player_name", value_key="touches", limit=limit, sort_keys=["touches", "goals", "wins"], minimum_games=player_activity_min_games),
            "most_demos": _top_rows(player_rows, label_key="player_name", value_key="demos", limit=limit, sort_keys=["demos", "wins", "touches"], minimum_games=player_activity_min_games),
            "best_scoring_rate": _top_rows(player_rows, label_key="player_name", value_key="goals_per_game", limit=limit, sort_keys=["goals_per_game", "goals", "wins"], minimum_games=player_min_games),
            "best_avg_score": _top_rows(player_rows, label_key="player_name", value_key="avg_score", limit=limit, sort_keys=["avg_score", "goals", "wins"], minimum_games=player_min_games),
            "best_win_rate": _top_rows(
                player_with_win_rate,
                label_key="player_name",
                value_key="win_rate",
                limit=limit,
                sort_keys=["win_rate", "wins", "goals"],
                minimum_games=player_min_games,
            ),
            "rlcs_most_goals": _top_rows(rlcs_player_rows, label_key="player_name", value_key="rlcs_goals", limit=limit, sort_keys=["rlcs_goals", "rlcs_wins", "goals"], minimum_games=2),
            "rlcs_best_goal_rate": _top_rows(rlcs_player_goal_rate_rows, label_key="player_name", value_key="goals_per_game", limit=limit, sort_keys=["goals_per_game", "rlcs_goals", "goals"], minimum_games=2),
        },
        "matchup_leaders": {
            "teams": _team_matchup_rows(con, limit=limit),
            "players": _player_matchup_rows(con, limit=limit),
        },
        "roster_leaders": {
            "most_wins": _top_rows(roster_rows, label_key="roster_name", value_key="wins", limit=limit, sort_keys=["wins", "goal_diff", "games"], minimum_games=roster_min_games),
            "best_win_rate": _top_rows(
                roster_with_win_rate,
                label_key="roster_name",
                value_key="win_rate",
                limit=limit,
                sort_keys=["win_rate", "wins", "goal_diff"],
                minimum_games=roster_min_games,
            ),
            "best_goal_diff": _top_rows(roster_rows, label_key="roster_name", value_key="goal_diff", limit=limit, sort_keys=["goal_diff", "wins", "games"], minimum_games=roster_min_games),
            "rlcs_most_wins": _top_rows(rlcs_roster_rows, label_key="roster_name", value_key="rlcs_wins", limit=limit, sort_keys=["rlcs_wins", "wins", "goal_diff"], minimum_games=2),
            "rlcs_best_win_rate": _top_rows(rlcs_roster_with_win_rate, label_key="roster_name", value_key="win_rate", limit=limit, sort_keys=["win_rate", "rlcs_wins", "wins"], minimum_games=2),
        },
        "event_leaders": {
            "most_wins": [
                {
                    "name": f"{row['team_name']} - {row['tournament_name']}",
                    "value": row["wins"],
                    "games": row["games"],
                    "wins": row["wins"],
                    "losses": row["losses"],
                }
                for row in event_rows[:limit]
            ],
            "rlcs_most_wins": [
                {
                    "name": f"{row['team_name']} - {row['tournament_name']}",
                    "value": row["wins"],
                    "games": row["games"],
                    "wins": row["wins"],
                    "losses": row["losses"],
                }
                for row in rlcs_event_rows[:limit]
            ],
            "rlcs_best_win_rate": [
                {
                    "name": f"{row['team_name']} - {row['tournament_name']}",
                    "value": row["win_rate"],
                    "games": row["games"],
                    "wins": row["wins"],
                    "losses": row["losses"],
                    "confidence": round(min(1.0, int(row.get("games") or 0) / 2), 3),
                }
                for row in sorted(
                    [row for row in rlcs_event_with_win_rate if int(row.get("games") or 0) >= 2],
                    key=lambda item: (item.get("win_rate") or 0, item.get("wins") or 0, item.get("goal_diff") or 0),
                    reverse=True,
                )[:limit]
            ],
        },
    }


def team_record_profile(con: duckdb.DuckDBPyConnection, team_name: str) -> dict[str, Any]:
    resolver = IdentityResolver(con)
    team_rows = _team_summary_rows(con)
    options = _known_team_names(con)
    resolved = _resolve_name(options, resolver.canonical_team_name(team_name))
    if resolved is None:
        raise ValueError(f"Unknown team name: {team_name}")

    match_rows = []
    for row in _canonical_matches(con):
        if row["blue_team_name"] == resolved:
            match_rows.append(
                {
                    "replay_id": row["replay_id"],
                    "match_date": row["match_date"],
                    "team_name": resolved,
                    "opponent_name": row["orange_team_name"],
                    "goals_for": int(row["blue_goals"] or 0),
                    "goals_against": int(row["orange_goals"] or 0),
                }
            )
        elif row["orange_team_name"] == resolved:
            match_rows.append(
                {
                    "replay_id": row["replay_id"],
                    "match_date": row["match_date"],
                    "team_name": resolved,
                    "opponent_name": row["blue_team_name"],
                    "goals_for": int(row["orange_goals"] or 0),
                    "goals_against": int(row["blue_goals"] or 0),
                }
            )
    summary = next((row for row in team_rows if row["team_name"].lower() == resolved.lower()), None)
    streaks = _team_streaks(match_rows)

    players: dict[str, dict[str, Any]] = {}
    roster_games: dict[str, dict[str, Any]] = {}
    for row in _canonical_player_matches(con):
        if row["team_name"] != resolved:
            continue
        bucket = players.setdefault(
            row["player_key"],
            {"player_name": row["player_name"], "games": 0, "wins": 0, "goals": 0, "touches": 0, "demos": 0},
        )
        if row["team_color"] == "blue":
            goals_for = int(row["blue_goals"])
            goals_against = int(row["orange_goals"])
        else:
            goals_for = int(row["orange_goals"])
            goals_against = int(row["blue_goals"])
        bucket["games"] += 1
        bucket["wins"] += int(goals_for > goals_against)
        bucket["goals"] += int(row.get("goals") or 0)
        bucket["touches"] += int(row.get("touches") or 0)
        bucket["demos"] += int(row.get("demos") or 0)
    rows_by_replay: dict[str, list[dict[str, Any]]] = {}
    for row in _canonical_player_matches(con):
        if row["team_name"] == resolved:
            rows_by_replay.setdefault(row["replay_id"], []).append(row)
    match_meta_by_replay = {row["replay_id"]: row for row in _canonical_matches(con) if resolved in {row["blue_team_name"], row["orange_team_name"]}}
    for replay_id, roster_rows in rows_by_replay.items():
        lineup = sorted({row["player_name"] for row in roster_rows if row.get("player_name")}, key=str.casefold)
        roster_name = " / ".join(lineup) if lineup else resolved
        meta = match_meta_by_replay.get(replay_id)
        if roster_rows[0]["team_color"] == "blue":
            goals_for = int(roster_rows[0]["blue_goals"])
            goals_against = int(roster_rows[0]["orange_goals"])
        else:
            goals_for = int(roster_rows[0]["orange_goals"])
            goals_against = int(roster_rows[0]["blue_goals"])
        bucket = roster_games.setdefault(
            roster_name,
            {"roster_name": roster_name, "games": 0, "wins": 0, "goal_diff": 0, "last_played": None, "tournament_name": meta.get("tournament_name") if meta else None},
        )
        bucket["games"] += 1
        bucket["wins"] += int(goals_for > goals_against)
        bucket["goal_diff"] += goals_for - goals_against
        if meta and meta.get("match_date") and (bucket["last_played"] is None or meta["match_date"] > bucket["last_played"]):
            bucket["last_played"] = meta["match_date"]
    player_rows = sorted(players.values(), key=lambda row: (row["games"], row["goals"], row["touches"], row["player_name"]), reverse=True)[:8]
    roster_rows = sorted(roster_games.values(), key=lambda row: (row["wins"], row["games"], row["goal_diff"], row["roster_name"]), reverse=True)[:6]

    frequency_row = {
        "telemetry_games": 0,
        "goals": 0,
        "touches": 0,
        "demos": 0,
        "pressure_phases": 0,
        "turnovers_forced": 0,
        "turnovers_committed": 0,
        "kickoff_wins": 0,
        "starvation_windows": 0,
    }
    seen_team_replays: set[str] = set()
    for event in _canonical_parsed_events(con):
        if event.get("team_name") == resolved:
            seen_team_replays.add(event["replay_id"])
            key = event.get("event_type")
            if key == "goal":
                frequency_row["goals"] += 1
            elif key == "touch":
                frequency_row["touches"] += 1
            elif key == "demo":
                frequency_row["demos"] += 1
            elif key == "pressure_phase":
                frequency_row["pressure_phases"] += 1
            elif key == "turnover":
                frequency_row["turnovers_forced"] += 1
            elif key == "kickoff_outcome":
                frequency_row["kickoff_wins"] += 1
            elif key == "boost_starvation_window":
                frequency_row["starvation_windows"] += 1
        if event.get("other_team_name") == resolved and event.get("event_type") == "turnover":
            seen_team_replays.add(event["replay_id"])
            frequency_row["turnovers_committed"] += 1
    frequency_row["telemetry_games"] = len(seen_team_replays)

    recent_matches = []
    for row in reversed(match_rows[-10:]):
        goals_for = int(row["goals_for"])
        goals_against = int(row["goals_against"])
        recent_matches.append(
            {
                **row,
                "result": "win" if goals_for > goals_against else "loss" if goals_for < goals_against else "draw",
                "scoreline": f"{goals_for}-{goals_against}",
            }
        )
    return {
        "team_name": resolved,
        "record": {
            **(
                summary
                or {
                    "team_name": resolved,
                    "games": 0,
                    "wins": 0,
                    "losses": 0,
                    "goals_for": 0,
                    "goals_against": 0,
                    "goal_diff": 0,
                    "goals_per_game": 0.0,
                    "shutouts": 0,
                    "last_played": None,
                }
            ),
            "win_rate": round((int((summary or {}).get("wins") or 0) / int((summary or {}).get("games") or 0)) if int((summary or {}).get("games") or 0) else 0.0, 4),
            **streaks,
            "has_history": bool(summary and int(summary.get("games") or 0)),
        },
        "players": player_rows,
        "rosters": roster_rows,
        "frequencies": _frequency_items(frequency_row),
        "telemetry_games": int(frequency_row.get("telemetry_games") or 0),
        "recent_matches": recent_matches,
        "leaderboard_snapshot": _team_live_snapshot(con, resolved),
    }


def player_record_profile(con: duckdb.DuckDBPyConnection, player_name: str) -> dict[str, Any]:
    resolver = IdentityResolver(con)
    player_rows = _player_summary_rows(con)
    known_players = _known_player_rows(con)
    options = _dedupe_display_names([row["player_name"] for row in known_players if row.get("player_name")])
    resolved = _resolve_name(options, resolver.resolve_player(None, player_name)["player_name"])
    if resolved is None:
        raise ValueError(f"Unknown player name: {player_name}")
    matching_known = [row for row in known_players if row["player_name"].lower() == resolved.lower()]
    known_summary = matching_known[0] if matching_known else None
    matching_keys = {row.get("player_key") for row in matching_known if row.get("player_key")}
    matching_summary_rows = [row for row in player_rows if row["player_name"].lower() == resolved.lower()]
    for row in matching_summary_rows:
        if row.get("player_key"):
            matching_keys.add(row["player_key"])
    if not matching_keys:
        raise ValueError(f"Unknown player name: {player_name}")
    primary_player_key = next(iter(matching_keys))
    summary = None
    if matching_summary_rows:
        summary = {
            "player_key": primary_player_key,
            "player_name": resolved,
            "platform": next((row.get("platform") for row in matching_summary_rows if row.get("platform")), (known_summary or {}).get("platform")),
            "platform_player_id": next((row.get("platform_player_id") for row in matching_summary_rows if row.get("platform_player_id")), (known_summary or {}).get("platform_player_id")),
            "games": sum(int(row.get("games") or 0) for row in matching_summary_rows),
            "wins": sum(int(row.get("wins") or 0) for row in matching_summary_rows),
            "losses": sum(int(row.get("losses") or 0) for row in matching_summary_rows),
            "goals": sum(int(row.get("goals") or 0) for row in matching_summary_rows),
            "touches": sum(int(row.get("touches") or 0) for row in matching_summary_rows),
            "demos": sum(int(row.get("demos") or 0) for row in matching_summary_rows),
            "pressure_phases": sum(int(row.get("pressure_phases") or 0) for row in matching_summary_rows),
            "turnovers_forced": sum(int(row.get("turnovers_forced") or 0) for row in matching_summary_rows),
            "kickoff_wins": sum(int(row.get("kickoff_wins") or 0) for row in matching_summary_rows),
            "starvation_windows": sum(int(row.get("starvation_windows") or 0) for row in matching_summary_rows),
            "shots": sum(int(row.get("shots") or 0) for row in matching_summary_rows),
            "saves": sum(int(row.get("saves") or 0) for row in matching_summary_rows),
            "score_total": sum(float(row.get("score_total") or 0.0) for row in matching_summary_rows),
            "avg_score": 0.0,
            "last_played": max((row.get("last_played") for row in matching_summary_rows if row.get("last_played")), default=None),
        }
        summary["avg_score"] = round((summary["score_total"] / summary["games"]) if summary["games"] else 0.0, 3)

    match_rows = [row for row in _canonical_player_matches(con) if row["player_key"] in matching_keys]
    match_rows.sort(key=lambda row: ((row.get("match_date") or ""), row.get("replay_id") or "", row.get("player_name") or ""))

    teammates: dict[str, dict[str, Any]] = {}
    opponents: dict[str, dict[str, Any]] = {}
    rows_by_replay: dict[str, list[dict[str, Any]]] = {}
    for row in _canonical_player_matches(con):
        rows_by_replay.setdefault(row["replay_id"], []).append(row)
    for row in match_rows:
        for peer in rows_by_replay.get(row["replay_id"], []):
            if peer["player_key"] in matching_keys:
                continue
            if peer["team_color"] == row["team_color"]:
                bucket = teammates.setdefault(
                    peer["player_key"],
                    {"teammate_name": peer["player_name"], "games": 0, "wins": 0, "teammate_goals": 0},
                )
                if row["team_color"] == "blue":
                    goals_for = int(row["blue_goals"])
                    goals_against = int(row["orange_goals"])
                else:
                    goals_for = int(row["orange_goals"])
                    goals_against = int(row["blue_goals"])
                bucket["games"] += 1
                bucket["wins"] += int(goals_for > goals_against)
                bucket["teammate_goals"] += int(peer.get("goals") or 0)
            else:
                bucket = opponents.setdefault(
                    peer["player_key"],
                    {
                        "opponent_name": peer["player_name"],
                        "games": 0,
                        "wins": 0,
                        "player_goals": 0,
                        "opponent_goals": 0,
                    },
                )
                if row["team_color"] == "blue":
                    goals_for = int(row["blue_goals"])
                    goals_against = int(row["orange_goals"])
                else:
                    goals_for = int(row["orange_goals"])
                    goals_against = int(row["blue_goals"])
                bucket["games"] += 1
                bucket["wins"] += int(goals_for > goals_against)
                bucket["player_goals"] += int(row.get("goals") or 0)
                bucket["opponent_goals"] += int(peer.get("goals") or 0)

    frequency_row = {
        "telemetry_games": 0,
        "goals": 0,
        "touches": 0,
        "demos": 0,
        "pressure_phases": 0,
        "turnovers_forced": 0,
        "turnovers_committed": 0,
        "kickoff_wins": 0,
        "starvation_windows": 0,
        "shots": 0,
        "saves": 0,
    }
    seen_player_replays: set[str] = set()
    for event in _canonical_parsed_events(con):
        if event.get("player_key") in matching_keys:
            seen_player_replays.add(event["replay_id"])
            key = event.get("event_type")
            if key == "goal":
                frequency_row["goals"] += 1
            elif key == "touch":
                frequency_row["touches"] += 1
            elif key == "demo":
                frequency_row["demos"] += 1
            elif key == "pressure_phase":
                frequency_row["pressure_phases"] += 1
            elif key == "turnover":
                frequency_row["turnovers_forced"] += 1
            elif key == "kickoff_outcome":
                frequency_row["kickoff_wins"] += 1
            elif key == "boost_starvation_window":
                frequency_row["starvation_windows"] += 1
        if event.get("other_player_key") in matching_keys and event.get("event_type") == "turnover":
            seen_player_replays.add(event["replay_id"])
            frequency_row["turnovers_committed"] += 1
    frequency_row["telemetry_games"] = len(seen_player_replays)

    recent_matches = []
    for row in reversed(match_rows[-10:]):
        if row["team_color"] == "blue":
            goals_for = int(row["blue_goals"])
            goals_against = int(row["orange_goals"])
        else:
            goals_for = int(row["orange_goals"])
            goals_against = int(row["blue_goals"])
        recent_matches.append(
            {
                **row,
                "opponent_name": row["orange_team_name"] if row["team_color"] == "blue" else row["blue_team_name"],
                "goals_for": goals_for,
                "goals_against": goals_against,
                "result": "win" if goals_for > goals_against else "loss" if goals_for < goals_against else "draw",
                "scoreline": f"{goals_for}-{goals_against}",
            }
        )
    return {
        "player_name": resolved,
        "platform": (summary or known_summary or {}).get("platform"),
        "platform_player_id": (summary or known_summary or {}).get("platform_player_id"),
        "tracker_profile_url": _tracker_profile_url((summary or known_summary or {}).get("platform"), (summary or known_summary or {}).get("platform_player_id"), resolved),
        "record": {
            **(
                summary
                or {
                    "player_key": primary_player_key,
                    "player_keys": sorted(matching_keys),
                    "player_name": resolved,
                    "platform": (known_summary or {}).get("platform"),
                    "platform_player_id": (known_summary or {}).get("platform_player_id"),
                    "games": 0,
                    "wins": 0,
                    "losses": 0,
                    "goals": 0,
                    "touches": 0,
                    "demos": 0,
                    "turnovers_forced": 0,
                    "kickoff_wins": 0,
                    "starvation_windows": 0,
                    "shots": 0,
                    "saves": 0,
                    "avg_score": 0.0,
                    "score_total": 0.0,
                    "last_played": None,
                }
            ),
            "win_rate": round((int((summary or {}).get("wins") or 0) / int((summary or {}).get("games") or 0)) if int((summary or {}).get("games") or 0) else 0.0, 4),
            "goals_per_game": round((int((summary or {}).get("goals") or 0) / int((summary or {}).get("games") or 0)) if int((summary or {}).get("games") or 0) else 0.0, 3),
            "touches_per_game": round((int((summary or {}).get("touches") or 0) / int((summary or {}).get("games") or 0)) if int((summary or {}).get("games") or 0) else 0.0, 3),
            "demos_per_game": round((int((summary or {}).get("demos") or 0) / int((summary or {}).get("games") or 0)) if int((summary or {}).get("games") or 0) else 0.0, 3),
            "has_history": bool(summary and int(summary.get("games") or 0)),
        },
        "teammates": sorted(teammates.values(), key=lambda row: (row["games"], row["wins"], row["teammate_goals"], row["teammate_name"]), reverse=True)[:8],
        "opponents": sorted(opponents.values(), key=lambda row: (row["games"], row["wins"], row["player_goals"], row["opponent_name"]), reverse=True)[:8],
        "frequencies": _frequency_items(frequency_row),
        "telemetry_games": int(frequency_row.get("telemetry_games") or 0),
        "recent_matches": recent_matches,
    }


def head_to_head(con: duckdb.DuckDBPyConnection, kind: str, left_name: str, right_name: str) -> dict[str, Any]:
    resolver = IdentityResolver(con)
    if kind not in {"team", "player"}:
        raise ValueError(f"Unsupported head-to-head kind: {kind}")
    if kind == "team":
        team_options = _known_team_names(con)
        left = _resolve_name(team_options, resolver.canonical_team_name(left_name))
        right = _resolve_name(team_options, resolver.canonical_team_name(right_name))
        if left is None:
            raise ValueError(f"Unknown team name: {left_name}")
        if right is None:
            raise ValueError(f"Unknown team name: {right_name}")
        match_rows = [
            row for row in _canonical_matches(con)
            if {row["blue_team_name"], row["orange_team_name"]} == {left, right}
        ]
        left_wins = 0
        right_wins = 0
        left_goals = 0
        right_goals = 0
        meetings = []
        for row in match_rows:
            if row["blue_team_name"] == left:
                goals_a = int(row["blue_goals"] or 0)
                goals_b = int(row["orange_goals"] or 0)
            else:
                goals_a = int(row["orange_goals"] or 0)
                goals_b = int(row["blue_goals"] or 0)
            left_goals += goals_a
            right_goals += goals_b
            if goals_a > goals_b:
                left_wins += 1
                winner = left
            elif goals_b > goals_a:
                right_wins += 1
                winner = right
            else:
                winner = "draw"
            meetings.append(
                {
                    **row,
                    "left_score": goals_a,
                    "right_score": goals_b,
                    "winner": winner,
                    "scoreline": f"{goals_a}-{goals_b}",
                }
            )
        return {
            "kind": "team",
            "left_name": left,
            "right_name": right,
            "summary": {
                "games": len(match_rows),
                "left_wins": left_wins,
                "right_wins": right_wins,
                "left_goals": left_goals,
                "right_goals": right_goals,
                "goal_diff": left_goals - right_goals,
                "last_played": meetings[-1]["match_date"] if meetings else None,
            },
            "meetings": list(reversed(meetings[-10:])),
        }

    player_rows = _player_summary_rows(con)
    known_players = _known_player_rows(con)
    player_options = _dedupe_display_names([row["player_name"] for row in known_players if row.get("player_name")])
    left = _resolve_name(player_options, resolver.resolve_player(None, left_name)["player_name"])
    right = _resolve_name(player_options, resolver.resolve_player(None, right_name)["player_name"])
    if left is None:
        raise ValueError(f"Unknown player name: {left_name}")
    if right is None:
        raise ValueError(f"Unknown player name: {right_name}")
    left_keys = {row["player_key"] for row in known_players if row.get("player_name", "").lower() == left.lower() and row.get("player_key")}
    right_keys = {row["player_key"] for row in known_players if row.get("player_name", "").lower() == right.lower() and row.get("player_key")}
    if not left_keys or not right_keys:
        raise ValueError(f"Unknown player pair: {left_name}, {right_name}")

    rows_by_replay: dict[str, list[dict[str, Any]]] = {}
    for row in _canonical_player_matches(con):
        rows_by_replay.setdefault(row["replay_id"], []).append(row)
    pair_rows: list[dict[str, Any]] = []
    for replay_id, replay_rows in rows_by_replay.items():
        left_row = next((row for row in replay_rows if row["player_key"] in left_keys), None)
        right_row = next((row for row in replay_rows if row["player_key"] in right_keys), None)
        if not left_row or not right_row:
            continue
        pair_rows.append(
            {
                "replay_id": replay_id,
                "match_date": left_row.get("match_date") or right_row.get("match_date"),
                "left_name": left,
                "right_name": right,
                "left_team_name": left_row["team_name"],
                "right_team_name": right_row["team_name"],
                "left_team_color": left_row["team_color"],
                "right_team_color": right_row["team_color"],
                "left_score": int(left_row["blue_goals"]) if left_row["team_color"] == "blue" else int(left_row["orange_goals"]),
                "right_score": int(right_row["blue_goals"]) if right_row["team_color"] == "blue" else int(right_row["orange_goals"]),
                "left_goals": int(left_row.get("goals") or 0),
                "right_goals": int(right_row.get("goals") or 0),
                "left_touches": int(left_row.get("touches") or 0),
                "right_touches": int(right_row.get("touches") or 0),
                "left_demos": int(left_row.get("demos") or 0),
                "right_demos": int(right_row.get("demos") or 0),
            }
        )
    pair_rows.sort(key=lambda row: ((row.get("match_date") or ""), row["replay_id"]))
    opposed = [row for row in pair_rows if row["left_team_color"] != row["right_team_color"]]
    teammates = [row for row in pair_rows if row["left_team_color"] == row["right_team_color"]]
    left_wins = sum(1 for row in opposed if int(row["left_score"]) > int(row["right_score"]))
    right_wins = sum(1 for row in opposed if int(row["right_score"]) > int(row["left_score"]))
    duo_wins = sum(1 for row in teammates if int(row["left_score"]) != int(row["right_score"]))
    return {
        "kind": "player",
        "left_name": left,
        "right_name": right,
        "summary": {
            "shared_games": len(pair_rows),
            "opposed_games": len(opposed),
            "teammate_games": len(teammates),
            "left_wins": left_wins,
            "right_wins": right_wins,
            "duo_wins": duo_wins,
            "left_goals": sum(int(row["left_goals"] or 0) for row in opposed),
            "right_goals": sum(int(row["right_goals"] or 0) for row in opposed),
            "left_touches": sum(int(row["left_touches"] or 0) for row in opposed),
            "right_touches": sum(int(row["right_touches"] or 0) for row in opposed),
        },
        "opposed_meetings": list(reversed(opposed[-10:])),
        "teammate_meetings": list(reversed(teammates[-10:])),
    }
