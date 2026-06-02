from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from .config import get_settings


CANONICAL_EVENT_TYPES = (
    "touch",
    "possession_start",
    "possession_end",
    "loose_ball_start",
    "turnover",
    "goal",
    "demo",
    "boost_starvation_window",
    "kickoff_outcome",
    "pressure_phase",
    "overcommit_proxy",
)


def _sql_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "''")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def refresh_warehouse(
    raw_db: Path | None = None,
    serving_db: Path | None = None,
    *,
    sample_limit: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    raw_db = Path(raw_db or settings.raw_db)
    serving_db = Path(serving_db or settings.serving_db)
    serving_db.parent.mkdir(parents=True, exist_ok=True)

    if not raw_db.exists():
        raise FileNotFoundError(f"Raw DuckDB database not found: {raw_db}")

    started = time.time()
    dataset_version = datetime.now(timezone.utc).strftime("replayos-%Y%m%dT%H%M%SZ")

    con = duckdb.connect(str(serving_db))
    try:
        con.execute(f"ATTACH '{_sql_path(raw_db)}' AS raw (READ_ONLY)")
        con.execute("CREATE SCHEMA IF NOT EXISTS main")
        con.execute("DROP VIEW IF EXISTS selected_replays")
        limit_sql = f"LIMIT {int(sample_limit)}" if sample_limit else ""
        con.execute(
            f"""
            CREATE TEMP VIEW selected_replays AS
            SELECT replay_id
            FROM raw.ingested
            ORDER BY replay_id
            {limit_sql}
            """
        )

        con.execute("DROP TABLE IF EXISTS refresh_log")
        con.execute(
            """
            CREATE TABLE refresh_log (
                refresh_id VARCHAR,
                raw_db VARCHAR,
                serving_db VARCHAR,
                dataset_version VARCHAR,
                sample_limit BIGINT,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                duration_seconds DOUBLE,
                status VARCHAR,
                notes VARCHAR
            )
            """
        )

        con.execute("DROP TABLE IF EXISTS replays")
        con.execute(
            f"""
            CREATE TABLE replays AS
            WITH event_bounds AS (
                SELECT replay_id, MIN(t) AS first_event_t, MAX(t) AS last_event_t
                FROM raw.derived_events
                WHERE replay_id IN (SELECT replay_id FROM selected_replays)
                GROUP BY replay_id
            )
            SELECT
                i.replay_id,
                'ballchasing' AS source,
                i.ts AS ingested_at,
                f.frame_game_duration AS game_duration,
                f.frame_total_frames AS frame_total_frames,
                eb.first_event_t,
                eb.last_event_t,
                CASE WHEN f.replay_id IS NULL THEN FALSE ELSE TRUE END AS has_semantic_features,
                '{dataset_version}' AS dataset_version,
                'raw.ingested -> replayos.replays' AS lineage
            FROM raw.ingested i
            JOIN selected_replays s USING (replay_id)
            LEFT JOIN raw.replay_semantic_features f USING (replay_id)
            LEFT JOIN event_bounds eb USING (replay_id)
            """
        )

        con.execute("DROP TABLE IF EXISTS events")
        con.execute(
            """
            CREATE TABLE events AS
            SELECT
                row_number() OVER (
                    ORDER BY replay_id, t, event_type, COALESCE(team, ''), COALESCE(player_id, '')
                ) AS event_id,
                replay_id,
                t,
                event_type,
                team AS team_color,
                CASE
                    WHEN team IN ('blue', 'orange') THEN replay_id || ':' || team
                    ELSE NULL
                END AS team_id,
                player_id,
                player_name,
                other_team AS other_team_color,
                CASE
                    WHEN other_team IN ('blue', 'orange') THEN replay_id || ':' || other_team
                    ELSE NULL
                END AS other_team_id,
                other_player_id,
                other_player_name,
                value,
                meta,
                'raw.derived_events -> replayos.events' AS lineage
            FROM raw.derived_events
            WHERE replay_id IN (SELECT replay_id FROM selected_replays)
            """
        )

        con.execute("DROP TABLE IF EXISTS features_replay")
        con.execute(
            """
            CREATE TABLE features_replay AS
            WITH goals AS (
                SELECT
                    replay_id,
                    SUM(CASE WHEN team_color = 'blue' AND event_type = 'goal' THEN 1 ELSE 0 END) AS blue_goals,
                    SUM(CASE WHEN team_color = 'orange' AND event_type = 'goal' THEN 1 ELSE 0 END) AS orange_goals
                FROM events
                GROUP BY replay_id
            )
            SELECT
                f.*,
                COALESCE(g.blue_goals, 0) AS blue_goals,
                COALESCE(g.orange_goals, 0) AS orange_goals,
                COALESCE(g.blue_goals, 0) - COALESCE(g.orange_goals, 0) AS goal_diff_blue,
                CASE
                    WHEN COALESCE(g.blue_goals, 0) > COALESCE(g.orange_goals, 0) THEN 1
                    WHEN COALESCE(g.blue_goals, 0) < COALESCE(g.orange_goals, 0) THEN 0
                    ELSE NULL
                END AS blue_win_label,
                'raw.replay_semantic_features + replayos.events -> replayos.features_replay' AS lineage
            FROM raw.replay_semantic_features f
            JOIN selected_replays s USING (replay_id)
            LEFT JOIN goals g USING (replay_id)
            """
        )

        con.execute("DROP TABLE IF EXISTS matches")
        con.execute(
            """
            CREATE TABLE matches AS
            WITH goals AS (
                SELECT replay_id, blue_goals, orange_goals, blue_win_label
                FROM features_replay
            )
            SELECT
                r.replay_id AS match_id,
                r.replay_id,
                r.replay_id || ':blue' AS blue_team_id,
                r.replay_id || ':orange' AS orange_team_id,
                r.game_duration,
                COALESCE(g.blue_goals, 0) AS blue_goals,
                COALESCE(g.orange_goals, 0) AS orange_goals,
                CASE
                    WHEN g.blue_win_label = 1 THEN r.replay_id || ':blue'
                    WHEN g.blue_win_label = 0 THEN r.replay_id || ':orange'
                    ELSE NULL
                END AS winner_team_id,
                'replay_id is the match id because source metadata does not expose series ids' AS join_strategy
            FROM replays r
            LEFT JOIN goals g USING (replay_id)
            """
        )

        con.execute("DROP TABLE IF EXISTS teams")
        con.execute(
            """
            CREATE TABLE teams AS
            SELECT
                blue_team_id AS team_id,
                replay_id,
                'blue' AS team_color,
                'Blue side - ' || substr(replay_id, 1, 8) AS team_name,
                blue_goals AS goals_for,
                orange_goals AS goals_against,
                CASE WHEN winner_team_id = blue_team_id THEN 1 WHEN winner_team_id IS NULL THEN NULL ELSE 0 END AS win_result,
                'synthetic side-team inferred from replay color' AS lineage
            FROM matches
            UNION ALL
            SELECT
                orange_team_id AS team_id,
                replay_id,
                'orange' AS team_color,
                'Orange side - ' || substr(replay_id, 1, 8) AS team_name,
                orange_goals AS goals_for,
                blue_goals AS goals_against,
                CASE WHEN winner_team_id = orange_team_id THEN 1 WHEN winner_team_id IS NULL THEN NULL ELSE 0 END AS win_result,
                'synthetic side-team inferred from replay color' AS lineage
            FROM matches
            """
        )

        con.execute("DROP TABLE IF EXISTS players")
        con.execute(
            """
            CREATE TABLE players AS
            SELECT
                player_id,
                COALESCE(any_value(player_name), player_id) AS player_name,
                COUNT(DISTINCT replay_id) AS replay_count,
                COUNT(*) AS event_count,
                SUM(CASE WHEN event_type = 'touch' THEN 1 ELSE 0 END) AS touches,
                SUM(CASE WHEN event_type = 'goal' THEN 1 ELSE 0 END) AS goals,
                SUM(CASE WHEN event_type = 'demo' THEN 1 ELSE 0 END) AS demos,
                MIN(replay_id) AS first_seen_replay_id,
                MAX(replay_id) AS last_seen_replay_id,
                'raw.derived_events player ids are source identifiers when display names are absent' AS lineage
            FROM events
            WHERE player_id IS NOT NULL
            GROUP BY player_id
            """
        )

        con.execute("DROP TABLE IF EXISTS features_team_match")
        con.execute(
            """
            CREATE TABLE features_team_match AS
            WITH base AS (
                SELECT
                    f.*,
                    COALESCE(f.blue_goals, 0) AS bg,
                    COALESCE(f.orange_goals, 0) AS og
                FROM features_replay f
            )
            SELECT
                replay_id || ':blue' AS team_id,
                replay_id,
                'blue' AS team_color,
                replay_id || ':orange' AS opponent_team_id,
                frame_blue_boost_early AS boost_early,
                frame_blue_boost_mid AS boost_mid,
                frame_blue_boost_late AS boost_late,
                frame_blue_boost_decay AS boost_decay,
                frame_poss_rate_blue AS possession_rate,
                frame_attack_zone_rate_blue AS attack_zone_rate,
                frame_clutch_poss_blue AS clutch_possession_rate,
                frame_clutch_boost_blue AS clutch_boost,
                frame_clutch_boost_advantage AS clutch_boost_advantage,
                frame_blue_aerial_rate AS aerial_rate,
                frame_blue_demos_total AS demos_total,
                frame_blue_demo_timing AS demo_timing,
                sem_touch_rate_blue AS touch_rate,
                sem_pressure_rate_blue AS pressure_rate,
                sem_starvation_rate_blue AS starvation_rate,
                sem_overcommit_rate_blue AS overcommit_rate,
                sem_goal_pressure_ratio_blue AS goal_pressure_ratio,
                bg AS goals_for,
                og AS goals_against,
                bg - og AS goal_diff,
                CASE WHEN bg > og THEN 1 WHEN bg < og THEN 0 ELSE NULL END AS win_result,
                'features_replay wide blue columns -> long team match row' AS lineage
            FROM base
            UNION ALL
            SELECT
                replay_id || ':orange' AS team_id,
                replay_id,
                'orange' AS team_color,
                replay_id || ':blue' AS opponent_team_id,
                frame_orange_boost_early AS boost_early,
                frame_orange_boost_mid AS boost_mid,
                frame_orange_boost_late AS boost_late,
                frame_orange_boost_decay AS boost_decay,
                CASE WHEN frame_poss_rate_blue IS NULL THEN NULL ELSE 1.0 - frame_poss_rate_blue END AS possession_rate,
                CASE WHEN frame_attack_zone_rate_blue IS NULL THEN NULL ELSE 1.0 - frame_attack_zone_rate_blue END AS attack_zone_rate,
                CASE WHEN frame_clutch_poss_blue IS NULL THEN NULL ELSE 1.0 - frame_clutch_poss_blue END AS clutch_possession_rate,
                frame_clutch_boost_orange AS clutch_boost,
                -frame_clutch_boost_advantage AS clutch_boost_advantage,
                frame_orange_aerial_rate AS aerial_rate,
                frame_orange_demos_total AS demos_total,
                frame_orange_demo_timing AS demo_timing,
                sem_touch_rate_orange AS touch_rate,
                sem_pressure_rate_orange AS pressure_rate,
                sem_starvation_rate_orange AS starvation_rate,
                sem_overcommit_rate_orange AS overcommit_rate,
                sem_goal_pressure_ratio_orange AS goal_pressure_ratio,
                og AS goals_for,
                bg AS goals_against,
                og - bg AS goal_diff,
                CASE WHEN og > bg THEN 1 WHEN og < bg THEN 0 ELSE NULL END AS win_result,
                'features_replay wide orange columns -> long team match row' AS lineage
            FROM base
            """
        )

        con.execute(
            """
            INSERT INTO events
            WITH base AS (
                SELECT COALESCE(MAX(event_id), 0) AS event_offset FROM events
            ),
            possessions AS (
                SELECT
                    replay_id,
                    t,
                    event_id,
                    team_color,
                    team_id,
                    player_id,
                    player_name,
                    LAG(team_color) OVER (PARTITION BY replay_id ORDER BY t, event_id) AS previous_team_color,
                    LAG(team_id) OVER (PARTITION BY replay_id ORDER BY t, event_id) AS previous_team_id
                FROM events
                WHERE event_type = 'possession_start'
            )
            SELECT
                base.event_offset + row_number() OVER (ORDER BY replay_id, t, event_id) AS event_id,
                replay_id,
                t,
                'turnover' AS event_type,
                team_color,
                team_id,
                player_id,
                player_name,
                previous_team_color AS other_team_color,
                previous_team_id AS other_team_id,
                NULL AS other_player_id,
                NULL AS other_player_name,
                1.0 AS value,
                '{"heuristic":"possession_start changed team"}' AS meta,
                'replayos.events possession transitions -> turnover' AS lineage
            FROM possessions, base
            WHERE previous_team_color IS NOT NULL AND previous_team_color <> team_color
            """
        )

        con.execute(
            """
            INSERT INTO events
            WITH base AS (
                SELECT COALESCE(MAX(event_id), 0) AS event_offset FROM events
            ),
            derived AS (
                SELECT replay_id, 0.0 AS t, 'kickoff_outcome' AS event_type, 'blue' AS team_color,
                       replay_id || ':blue' AS team_id, replay_id || ':orange' AS other_team_id,
                       CAST(frame_kickoff_wins_blue AS DOUBLE) AS value,
                       '{"heuristic":"aggregate kickoff wins from replay features"}' AS meta
                FROM features_replay
                WHERE COALESCE(frame_kickoff_wins_blue, 0) > 0
                UNION ALL
                SELECT replay_id, 0.0 AS t, 'kickoff_outcome' AS event_type, 'orange' AS team_color,
                       replay_id || ':orange' AS team_id, replay_id || ':blue' AS other_team_id,
                       CAST(frame_kickoff_wins_orange AS DOUBLE) AS value,
                       '{"heuristic":"aggregate kickoff wins from replay features"}' AS meta
                FROM features_replay
                WHERE COALESCE(frame_kickoff_wins_orange, 0) > 0
                UNION ALL
                SELECT replay_id, COALESCE(game_duration, 180.0) * 0.5 AS t, 'pressure_phase' AS event_type,
                       team_color, team_id, opponent_team_id AS other_team_id, pressure_rate AS value,
                       '{"heuristic":"aggregate pressure rate projected to replay midpoint"}' AS meta
                FROM features_team_match
                LEFT JOIN replays USING (replay_id)
                WHERE COALESCE(pressure_rate, 0) > 0
                UNION ALL
                SELECT replay_id, COALESCE(game_duration, 180.0) * 0.5 AS t, 'overcommit_proxy' AS event_type,
                       team_color, team_id, opponent_team_id AS other_team_id, overcommit_rate AS value,
                       '{"heuristic":"aggregate overcommit rate projected to replay midpoint"}' AS meta
                FROM features_team_match
                LEFT JOIN replays USING (replay_id)
                WHERE COALESCE(overcommit_rate, 0) > 0
            )
            SELECT
                base.event_offset + row_number() OVER (ORDER BY replay_id, event_type, team_color) AS event_id,
                replay_id,
                t,
                event_type,
                team_color,
                team_id,
                NULL AS player_id,
                NULL AS player_name,
                CASE WHEN team_color = 'blue' THEN 'orange' WHEN team_color = 'orange' THEN 'blue' ELSE NULL END AS other_team_color,
                other_team_id,
                NULL AS other_player_id,
                NULL AS other_player_name,
                value,
                meta,
                'replayos.features -> aggregate semantic event proxy' AS lineage
            FROM derived, base
            """
        )

        con.execute("DROP TABLE IF EXISTS features_player_match")
        con.execute(
            """
            CREATE TABLE features_player_match AS
            SELECT
                replay_id || ':' || COALESCE(player_id, 'unknown') AS player_match_id,
                replay_id,
                player_id,
                COALESCE(any_value(player_name), player_id) AS player_name,
                any_value(team_id) AS team_id,
                any_value(team_color) AS team_color,
                COUNT(*) AS events_total,
                SUM(CASE WHEN event_type = 'touch' THEN 1 ELSE 0 END) AS touches,
                SUM(CASE WHEN event_type = 'goal' THEN 1 ELSE 0 END) AS goals,
                SUM(CASE WHEN event_type = 'demo' THEN 1 ELSE 0 END) AS demos,
                SUM(CASE WHEN event_type = 'possession_start' THEN 1 ELSE 0 END) AS possessions_started,
                SUM(CASE WHEN event_type = 'boost_starvation_window' THEN 1 ELSE 0 END) AS boost_starvation_windows,
                AVG(value) AS avg_event_value,
                (
                    SUM(CASE WHEN event_type = 'touch' THEN 1 ELSE 0 END) * 0.35
                    + SUM(CASE WHEN event_type = 'goal' THEN 1 ELSE 0 END) * 4.0
                    + SUM(CASE WHEN event_type = 'demo' THEN 1 ELSE 0 END) * 1.5
                    + SUM(CASE WHEN event_type = 'possession_start' THEN 1 ELSE 0 END) * 0.7
                    - SUM(CASE WHEN event_type = 'boost_starvation_window' THEN 1 ELSE 0 END) * 0.8
                ) AS impact_score,
                'event counts and weighted contribution heuristic' AS lineage
            FROM events
            WHERE player_id IS NOT NULL
            GROUP BY replay_id, player_id
            """
        )

        con.execute("DROP TABLE IF EXISTS model_versions")
        con.execute(
            """
            CREATE TABLE model_versions (
                model_version_id VARCHAR PRIMARY KEY,
                name VARCHAR,
                model_type VARCHAR,
                target VARCHAR,
                dataset_version VARCHAR,
                features_json VARCHAR,
                split_logic VARCHAR,
                metrics_json VARCHAR,
                calibration_json VARCHAR,
                artifact_json VARCHAR,
                created_at TIMESTAMP
            )
            """
        )

        con.execute("DROP TABLE IF EXISTS experiment_runs")
        con.execute(
            """
            CREATE TABLE experiment_runs (
                experiment_id VARCHAR PRIMARY KEY,
                model_version_id VARCHAR,
                run_group VARCHAR,
                dataset_version VARCHAR,
                baseline_model_version_id VARCHAR,
                metrics_json VARCHAR,
                notes VARCHAR,
                created_at TIMESTAMP
            )
            """
        )

        con.execute("DROP TABLE IF EXISTS predictions")
        con.execute(
            """
            CREATE TABLE predictions (
                prediction_id VARCHAR PRIMARY KEY,
                replay_id VARCHAR,
                model_version_id VARCHAR,
                target_type VARCHAR,
                target_id VARCHAR,
                prediction_type VARCHAR,
                predicted_label VARCHAR,
                probability DOUBLE,
                score DOUBLE,
                reasons_json VARCHAR,
                created_at TIMESTAMP
            )
            """
        )

        con.execute("DROP TABLE IF EXISTS lineage")
        con.execute(
            """
            CREATE TABLE lineage (
                table_name VARCHAR,
                primary_key VARCHAR,
                join_strategy VARCHAR,
                refresh_cadence VARCHAR,
                source_tables VARCHAR,
                notes VARCHAR
            )
            """
        )
        lineage_rows = [
            ("replays", "replay_id", "raw.ingested replay_id", "batch refresh", "raw.ingested, raw.replay_semantic_features", "One row per replay file harvested from Ballchasing."),
            ("matches", "match_id", "match_id equals replay_id", "batch refresh", "replays, features_replay", "Series metadata is unavailable, so each replay is modeled as one match."),
            ("teams", "team_id", "synthetic replay_id:color", "batch refresh", "matches", "Actual organization names are not present; color-side teams are honest synthetic entities."),
            ("players", "player_id", "source player identifier", "batch refresh", "events", "Names fall back to source ids when display names are absent."),
            ("events", "event_id", "window row number over replay/timestamp/event/player", "batch refresh", "raw.derived_events", "Semantic events generated from frame telemetry."),
            ("features_replay", "replay_id", "replay_id", "batch refresh", "raw.replay_semantic_features, events", "Replay-level model features plus outcome labels from goal events."),
            ("features_team_match", "team_id", "replay_id:color", "batch refresh", "features_replay", "Wide blue/orange features normalized to team rows."),
            ("features_player_match", "player_match_id", "replay_id:player_id", "batch refresh", "events", "Player impact features from semantic event counts."),
            ("predictions", "prediction_id", "target_id + model_version_id", "model run", "features_replay, features_team_match", "Versioned model outputs and reason codes."),
            ("model_versions", "model_version_id", "generated run id", "model run", "analytics pipeline", "Stores dataset version, split logic, metrics, calibration, and artifacts."),
        ]
        con.executemany("INSERT INTO lineage VALUES (?, ?, ?, ?, ?, ?)", lineage_rows)

        con.execute("CREATE INDEX IF NOT EXISTS idx_replays_id ON replays(replay_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_events_replay ON events(replay_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_teams_id ON teams(team_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_players_id ON players(player_id)")

        completed = time.time()
        con.execute(
            "INSERT INTO refresh_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                dataset_version,
                str(raw_db),
                str(serving_db),
                dataset_version,
                sample_limit,
                datetime.fromtimestamp(started, timezone.utc),
                datetime.fromtimestamp(completed, timezone.utc),
                completed - started,
                "completed",
                _json({"event_types": CANONICAL_EVENT_TYPES}),
            ],
        )

        tables = [
            "replays",
            "matches",
            "teams",
            "players",
            "events",
            "features_replay",
            "features_team_match",
            "features_player_match",
        ]
        counts = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        return {
            "dataset_version": dataset_version,
            "raw_db": str(raw_db),
            "serving_db": str(serving_db),
            "duration_seconds": round(completed - started, 3),
            "counts": counts,
        }
    finally:
        try:
            con.execute("DETACH raw")
        except Exception:
            pass
        con.close()
