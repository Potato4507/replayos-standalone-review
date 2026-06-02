from __future__ import annotations

import json
import re
from typing import Any

import duckdb

from .analytics import compare_teams
from .identity import IdentityResolver, clean_identity_text, is_placeholder_team_name


def answer_question(con: duckdb.DuckDBPyConnection, question: str, *, replay_id: str | None = None) -> dict[str, Any]:
    normalized = question.lower().strip() or "summary"
    if any(token in normalized for token in ("weak", "exploit", "pressure", "fragile", "mistake", "blunder")):
        return _weakness_answer(con)
    if "player" in normalized or "impact" in normalized:
        return _player_impact_answer(con, replay_id)
    if any(token in normalized for token in ("momentum", "turning", "swing", "flip")):
        return _momentum_answer(con, replay_id)
    if "model" in normalized or "predict" in normalized or "calibration" in normalized:
        return _model_answer(con)
    if "matchup" in normalized or "compare" in normalized:
        teams = con.execute("SELECT team_id FROM features_team_match ORDER BY replay_id, team_color LIMIT 2").fetchall()
        if len(teams) < 2:
            return {"intent": "matchup", "answer": "No team-match feature rows are available yet.", "data": []}
        matchup = compare_teams(con, teams[0][0], teams[1][0])
        return {
            "intent": "matchup",
            "answer": f"{matchup['predicted_label']} has the edge at {matchup['team_a_win_probability']:.1%} for team A.",
            "data": matchup,
        }
    return _summary_answer(con)


def _summary_answer(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    counts = {
        "replays": con.execute("SELECT COUNT(*) FROM replays").fetchone()[0],
        "events": con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "team_match_features": con.execute("SELECT COUNT(*) FROM features_team_match").fetchone()[0],
        "predictions": con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0],
    }
    parsed_replays = _safe_count(con, "replay_parsed_status", "WHERE status = 'completed'")
    remote_replays = _safe_count(con, "remote_replays")
    leaderboard_rows = _safe_count(con, "live_leaderboards")
    return {
        "intent": "summary",
        "answer": f"{counts['replays']} replays are indexed. {parsed_replays} have named local parses, {remote_replays} came through Ballchasing, and {leaderboard_rows} live leaderboard rows are cached.",
        "data": {
            "counts": counts,
            "coverage": {
                "parsed_replays": parsed_replays,
                "remote_replays": remote_replays,
                "leaderboard_rows": leaderboard_rows,
            },
        },
    }


def _weakness_answer(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    if not _table_exists(con, "features_team_match"):
        fallback = _named_pressure_fallback(con)
        return {
            "intent": "opponent_weakness",
            "answer": "The clearest pressure targets are named teams whose replay telemetry trends toward boost starvation, overcommit, and weak late boost control." if fallback else "No team telemetry rows are available yet.",
            "data": fallback,
        }
    name_lookup = _named_team_lookup(con)
    rows = con.execute(
        """
        SELECT
            replay_id,
            team_color,
            starvation_rate,
            overcommit_rate,
            pressure_rate,
            clutch_boost_advantage,
            goals_for,
            goals_against
        FROM features_team_match
        WHERE team_color IN ('blue', 'orange')
        """
    ).fetchall()
    buckets: dict[str, dict[str, Any]] = {}
    for replay_id, team_color, starvation_rate, overcommit_rate, pressure_rate, clutch_boost_advantage, goals_for, goals_against in rows:
        context = name_lookup.get((replay_id, team_color))
        team_name = context["team_name"] if context else None
        if not _is_viable_team_name(team_name):
            continue
        bucket = buckets.setdefault(
            team_name,
            {
                "team_name": team_name,
                "matches": 0,
                "wins": 0,
                "losses": 0,
                "starvation_total": 0.0,
                "overcommit_total": 0.0,
                "pressure_total": 0.0,
                "clutch_total": 0.0,
                "rlcs_matches": 0,
                "latest_event": None,
            },
        )
        bucket["matches"] += 1
        bucket["wins"] += int((goals_for or 0) > (goals_against or 0))
        bucket["losses"] += int((goals_against or 0) > (goals_for or 0))
        bucket["starvation_total"] += float(starvation_rate or 0.0)
        bucket["overcommit_total"] += float(overcommit_rate or 0.0)
        bucket["pressure_total"] += float(pressure_rate or 0.0)
        bucket["clutch_total"] += float(clutch_boost_advantage or 0.0)
        if context and context.get("is_rlcs_context"):
            bucket["rlcs_matches"] += 1
        if context and context.get("tournament_name"):
            bucket["latest_event"] = context["tournament_name"]

    if not buckets:
        fallback = _named_pressure_fallback(con)
        return {
            "intent": "opponent_weakness",
            "answer": "The clearest pressure targets are named teams whose replay telemetry trends toward boost starvation, overcommit, and weak late boost control." if fallback else "No named team telemetry rows are available yet.",
            "data": fallback,
        }
    minimum_matches = 2 if any(int(row.get("matches") or 0) >= 2 for row in buckets.values()) else 1
    data = []
    for row in buckets.values():
        matches = int(row["matches"])
        if matches < minimum_matches:
            continue
        starvation_rate = row["starvation_total"] / matches
        overcommit_rate = row["overcommit_total"] / matches
        pressure_rate = row["pressure_total"] / matches
        clutch_boost_advantage = row["clutch_total"] / matches
        weakness_score = (
            starvation_rate * 1.9
            + overcommit_rate * 1.5
            + max(0.0, -clutch_boost_advantage / 18.0)
            + max(0.0, (0.48 - pressure_rate) * 0.6)
        )
        data.append(
            {
                "team_name": row["team_name"],
                "matches": matches,
                "wins": row["wins"],
                "losses": row["losses"],
                "starvation_rate": round(starvation_rate, 4),
                "overcommit_rate": round(overcommit_rate, 4),
                "pressure_rate": round(pressure_rate, 4),
                "clutch_boost_advantage": round(clutch_boost_advantage, 4),
                "weakness_score": round(weakness_score, 4),
                "rlcs_matches": row["rlcs_matches"],
                "latest_event": row.get("latest_event"),
            }
        )
    data.sort(
        key=lambda row: (
            row["weakness_score"],
            row["rlcs_matches"],
            row["matches"],
        ),
        reverse=True,
    )
    return {
        "intent": "opponent_weakness",
        "answer": "The clearest pressure targets are named teams whose replay telemetry trends toward boost starvation, overcommit, and weak late boost control.",
        "data": data[:8] if data else _named_pressure_fallback(con),
    }


def _player_impact_answer(con: duckdb.DuckDBPyConnection, replay_id: str | None) -> dict[str, Any]:
    if replay_id:
        try:
            cached = con.execute(
                """
                SELECT player_impact_json
                FROM replay_review_cache
                WHERE replay_id = ?
                LIMIT 1
                """,
                [replay_id],
            ).fetchone()
        except duckdb.Error:
            cached = None
        if cached and cached[0]:
            payload = json.loads(cached[0] or "[]")
            data = [
                {
                    "player_id": item.get("player_name"),
                    "player_name": item.get("player_name"),
                    "replay_id": replay_id,
                    "team_color": item.get("team_color"),
                    "impact_score": round(float(item.get("net_impact") or 0.0), 3),
                    "touches": int(item.get("touches") or 0),
                    "goals": int(item.get("goals") or 0),
                    "demos": int(item.get("demos") or 0),
                    "positive_swings": int(item.get("positive_swings") or 0),
                    "negative_swings": int(item.get("negative_swings") or 0),
                }
                for item in payload
            ]
            return {
                "intent": "player_impact",
                "answer": f"Replay review cache ranked the biggest net impact swings for replay {replay_id}.",
                "data": data[:10],
            }
    params: list[Any] = []
    where = ""
    if replay_id:
        where = "WHERE replay_id = ?"
        params.append(replay_id)
    rows = con.execute(
        f"""
        SELECT player_id, player_name, replay_id, team_color, impact_score, touches, goals, demos
        FROM features_player_match
        {where}
        ORDER BY impact_score DESC
        LIMIT 10
        """,
        params,
    ).fetchall()
    if not rows and replay_id:
        rows = con.execute(
            """
            SELECT
                COALESCE(player_id, player_name) AS player_id,
                player_name,
                replay_id,
                team_color,
                SUM(
                    CASE event_type
                        WHEN 'goal' THEN 1.0
                        WHEN 'touch' THEN 0.04
                        WHEN 'demo' THEN 0.2
                        WHEN 'pressure_phase' THEN 0.1
                        WHEN 'kickoff_outcome' THEN 0.08
                        WHEN 'turnover' THEN -0.12
                        ELSE 0.0
                    END
                ) AS impact_score,
                SUM(CASE WHEN event_type = 'touch' THEN 1 ELSE 0 END) AS touches,
                SUM(CASE WHEN event_type = 'goal' THEN 1 ELSE 0 END) AS goals,
                SUM(CASE WHEN event_type = 'demo' THEN 1 ELSE 0 END) AS demos
            FROM replay_parsed_events
            WHERE replay_id = ? AND player_name IS NOT NULL
            GROUP BY 1, 2, 3, 4
            ORDER BY impact_score DESC, goals DESC, touches DESC
            LIMIT 10
            """,
            [replay_id],
        ).fetchall()
    data = [
        {
            "player_id": row[0],
            "player_name": row[1],
            "replay_id": row[2],
            "team_color": row[3],
            "impact_score": round(float(row[4] or 0.0), 3),
            "touches": row[5],
            "goals": row[6],
            "demos": row[7],
        }
        for row in rows
    ]
    scope = f" for replay {replay_id}" if replay_id else ""
    return {"intent": "player_impact", "answer": f"Top player impact rows{scope} are ranked by touch, goal, demo, and possession events.", "data": data}


def _momentum_answer(con: duckdb.DuckDBPyConnection, replay_id: str | None) -> dict[str, Any]:
    if not replay_id:
        first = con.execute("SELECT replay_id FROM features_replay ORDER BY replay_id LIMIT 1").fetchone()
        replay_id = first[0] if first else None
    if not replay_id:
        return {"intent": "momentum", "answer": "No replay feature rows are available for momentum analysis.", "data": []}
    team_lookup = _named_team_lookup(con)
    rows = con.execute(
        """
        WITH bounds AS (
            SELECT replay_id, COALESCE(MAX(t), 1.0) AS max_t
            FROM events
            WHERE replay_id = ?
            GROUP BY replay_id
        )
        SELECT
            CASE
                WHEN e.t < b.max_t / 3 THEN 'early'
                WHEN e.t < 2 * b.max_t / 3 THEN 'mid'
                ELSE 'late'
            END AS segment,
            e.team_color,
            SUM(CASE WHEN event_type = 'touch' THEN 1 ELSE 0 END) AS touches,
            SUM(CASE WHEN event_type = 'goal' THEN 1 ELSE 0 END) AS goals,
            SUM(CASE WHEN event_type = 'demo' THEN 1 ELSE 0 END) AS demos,
            SUM(CASE WHEN event_type = 'boost_starvation_window' THEN 1 ELSE 0 END) AS starvation_windows
        FROM events e
        JOIN bounds b USING (replay_id)
        WHERE e.replay_id = ?
        GROUP BY segment, e.team_color
        ORDER BY segment, e.team_color
        """,
        [replay_id, replay_id],
    ).fetchall()
    data = [
        {
            "segment": row[0],
            "team_color": row[1],
            "team_name": (team_lookup.get((replay_id, row[1])) or {}).get("team_name"),
            "touches": row[2],
            "goals": row[3],
            "demos": row[4],
            "starvation_windows": row[5],
        }
        for row in rows
    ]
    return {"intent": "momentum", "answer": f"Momentum for {replay_id} is segmented into early, mid, and late pressure signals.", "data": data}


def _model_answer(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rows = con.execute(
        """
        SELECT model_version_id, name, model_type, target, metrics_json, calibration_json, created_at
        FROM model_versions
        ORDER BY created_at DESC
        LIMIT 5
        """
    ).fetchall()
    data = []
    for row in rows:
        data.append(
            {
                "model_version_id": row[0],
                "name": row[1],
                "model_type": row[2],
                "target": row[3],
                "metrics": json.loads(row[4] or "{}"),
                "calibration": json.loads(row[5] or "[]"),
                "created_at": row[6].isoformat() if hasattr(row[6], "isoformat") else row[6],
            }
        )
    return {"intent": "model_evaluation", "answer": "Model runs store split logic, metrics, calibration bins, and artifacts in model_versions.", "data": data}


def _safe_count(con: duckdb.DuckDBPyConnection, table_name: str, filters: str = "") -> int:
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {table_name} {filters}").fetchone()[0] or 0)
    except duckdb.Error:
        return 0


def _table_exists(con: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    try:
        row = con.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'main' AND table_name = ?
            LIMIT 1
            """,
            [table_name],
        ).fetchone()
    except duckdb.Error:
        return False
    return bool(row)


def _is_viable_team_name(name: str | None) -> bool:
    cleaned = clean_identity_text(name)
    if not cleaned or cleaned == "Unknown" or is_placeholder_team_name(cleaned):
        return False
    if re.fullmatch(r"team ?\d+", cleaned.casefold()):
        return False
    return sum(1 for char in cleaned if char.isalpha()) >= 2


def _named_team_lookup(con: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], dict[str, Any]]:
    resolver = IdentityResolver(con)
    lookup: dict[tuple[str, str], dict[str, Any]] = {}

    def merge_row(
        replay_id: Any,
        team_color: str,
        team_name: Any,
        title: Any = None,
        season_type: Any = None,
        group_names_json: Any = None,
    ) -> None:
        if not replay_id:
            return
        canonical = resolver.canonical_team_name(team_name)
        if not _is_viable_team_name(canonical):
            return
        haystack = " ".join(
            str(value or "")
            for value in (
                title,
                season_type,
                " ".join(_json_list(group_names_json)),
            )
        ).casefold()
        tournament_name = None
        if group_names_json:
            names = _json_list(group_names_json)
            tournament_name = names[0] if names else None
        elif title:
            tournament_name = str(title)
        lookup[(str(replay_id), team_color)] = {
            "team_name": canonical,
            "tournament_name": tournament_name,
            "is_rlcs_context": any(token in haystack for token in ("rlcs", "major", "regional", "qualifier", "world", "ewc")),
        }

    if _table_exists(con, "remote_replays"):
        for row in con.execute(
            """
            SELECT replay_id, blue_team_name, orange_team_name, title, season_type, group_names_json
            FROM remote_replays
            """
        ).fetchall():
            merge_row(row[0], "blue", row[1], row[3], row[4], row[5])
            merge_row(row[0], "orange", row[2], row[3], row[4], row[5])
    if _table_exists(con, "replay_parsed_status"):
        for row in con.execute(
            """
            SELECT replay_id, blue_team_name, orange_team_name
            FROM replay_parsed_status
            WHERE status = 'completed'
            """
        ).fetchall():
            lookup.setdefault((str(row[0]), "blue"), {"team_name": resolver.canonical_team_name(row[1]), "tournament_name": None, "is_rlcs_context": False})
            lookup.setdefault((str(row[0]), "orange"), {"team_name": resolver.canonical_team_name(row[2]), "tournament_name": None, "is_rlcs_context": False})
    return {
        key: value
        for key, value in lookup.items()
        if _is_viable_team_name(value.get("team_name"))
    }


def _json_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed if item]


def _named_pressure_fallback(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    if not (_table_exists(con, "replay_parsed_events") and _table_exists(con, "replay_parsed_status")):
        return []
    rows = con.execute(
        """
        SELECT
            pe.replay_id,
            pe.event_type,
            pe.team_color,
            pe.other_team_color,
            ps.blue_team_name,
            ps.orange_team_name,
            ps.blue_goals,
            ps.orange_goals
        FROM replay_parsed_events pe
        JOIN replay_parsed_status ps USING (replay_id)
        WHERE ps.status = 'completed'
        """
    ).fetchall()
    resolver = IdentityResolver(con)
    by_replay_team: dict[tuple[str, str], dict[str, Any]] = {}
    for replay_id, event_type, team_color, other_team_color, blue_team_name, orange_team_name, blue_goals, orange_goals in rows:
        if team_color not in {"blue", "orange"}:
            continue
        names = {
            "blue": resolver.canonical_team_name(blue_team_name),
            "orange": resolver.canonical_team_name(orange_team_name),
        }
        team_name = names.get(team_color)
        if not _is_viable_team_name(team_name):
            continue
        bucket = by_replay_team.setdefault(
            (str(replay_id), team_color),
            {
                "team_name": team_name,
                "turnovers_forced": 0,
                "turnovers_committed": 0,
                "pressure_phases": 0,
                "touches": 0,
                "goals_for": int(blue_goals or 0) if team_color == "blue" else int(orange_goals or 0),
                "goals_against": int(orange_goals or 0) if team_color == "blue" else int(blue_goals or 0),
            },
        )
        if event_type == "touch":
            bucket["touches"] += 1
        elif event_type == "pressure_phase":
            bucket["pressure_phases"] += 1
        elif event_type == "turnover":
            bucket["turnovers_forced"] += 1
            if other_team_color in {"blue", "orange"}:
                other_team_name = names.get(other_team_color)
                if _is_viable_team_name(other_team_name):
                    other_bucket = by_replay_team.setdefault(
                        (str(replay_id), other_team_color),
                        {
                            "team_name": other_team_name,
                            "turnovers_forced": 0,
                            "turnovers_committed": 0,
                            "pressure_phases": 0,
                            "touches": 0,
                            "goals_for": int(blue_goals or 0) if other_team_color == "blue" else int(orange_goals or 0),
                            "goals_against": int(orange_goals or 0) if other_team_color == "blue" else int(blue_goals or 0),
                        },
                    )
                    other_bucket["turnovers_committed"] += 1
    aggregate: dict[str, dict[str, Any]] = {}
    for row in by_replay_team.values():
        team_name = row["team_name"]
        bucket = aggregate.setdefault(
            team_name,
            {
                "team_name": team_name,
                "matches": 0,
                "wins": 0,
                "losses": 0,
                "turnovers_committed_total": 0,
                "pressure_total": 0,
                "touch_total": 0,
                "turnovers_forced_total": 0,
            },
        )
        bucket["matches"] += 1
        bucket["wins"] += int(row["goals_for"] > row["goals_against"])
        bucket["losses"] += int(row["goals_against"] > row["goals_for"])
        bucket["turnovers_committed_total"] += int(row["turnovers_committed"])
        bucket["pressure_total"] += int(row["pressure_phases"])
        bucket["touch_total"] += int(row["touches"])
        bucket["turnovers_forced_total"] += int(row["turnovers_forced"])
    minimum_matches = 2 if any(int(row.get("matches") or 0) >= 2 for row in aggregate.values()) else 1
    data = []
    for row in aggregate.values():
        matches = int(row["matches"])
        if matches < minimum_matches:
            continue
        touches = max(int(row["touch_total"] or 0), 1)
        turnovers_committed_rate = float(row["turnovers_committed_total"]) / matches
        pressure_rate = float(row["pressure_total"]) / touches
        weakness_score = turnovers_committed_rate * 0.9 + max(0.0, (0.18 - pressure_rate) * 8.0) + (float(row["losses"]) / matches) * 0.45
        data.append(
            {
                "team_name": row["team_name"],
                "matches": matches,
                "wins": row["wins"],
                "losses": row["losses"],
                "starvation_rate": None,
                "overcommit_rate": round(turnovers_committed_rate, 3),
                "pressure_rate": round(pressure_rate, 3),
                "clutch_boost_advantage": None,
                "weakness_score": round(weakness_score, 4),
                "rlcs_matches": 0,
                "latest_event": "parsed replay pressure fallback",
            }
        )
    data.sort(key=lambda row: (row["weakness_score"], row["matches"]), reverse=True)
    return data[:8]


def matchup_report_markdown(matchup: dict[str, Any]) -> str:
    lines = [
        "# ReplayOS Matchup Report",
        "",
        f"Team A: `{matchup['team_a_id']}`",
        f"Team B: `{matchup['team_b_id']}`",
        "",
        f"Team A win probability: **{matchup['team_a_win_probability']:.1%}**",
        f"Predicted edge: `{matchup['predicted_label']}`",
        "",
        "## Reason Codes",
    ]
    for reason in matchup["reason_codes"]:
        lines.append(
            f"- `{reason['feature']}`: A={reason['team_a']}, B={reason['team_b']}, contribution={reason['contribution']}"
        )
    lines.extend(["", "## Assumption", matchup["assumption"]])
    return "\n".join(lines)
