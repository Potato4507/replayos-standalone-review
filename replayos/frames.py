from __future__ import annotations

import json
import math
import zlib
from itertools import permutations
from pathlib import Path
from typing import Any

import duckdb

from .ballchasing import ensure_ballchasing_replay_download
from .car_meta import car_profile
from .carball_ingest import BOOST_PAD_LAYOUT, load_parsed_replay_frames
from .config import get_settings
from .db import database_connection


FRAME_PAYLOAD_VERSION = 3


def ensure_frame_cache_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS replay_frame_cache (
            replay_id VARCHAR,
            sample_hz BIGINT,
            max_frames BIGINT,
            frame_count BIGINT,
            payload_json VARCHAR,
            created_at TIMESTAMP,
            PRIMARY KEY (replay_id, sample_hz, max_frames)
        )
        """
    )
    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_replay_frame_cache_created_at
        ON replay_frame_cache (created_at)
        """
    )


def prune_frame_cache(con: duckdb.DuckDBPyConnection, *, keep_entries: int | None = None) -> None:
    settings = get_settings()
    keep_entries = int(keep_entries or settings.frame_cache_entries)
    count = con.execute("SELECT COUNT(*) FROM replay_frame_cache").fetchone()[0]
    if int(count or 0) <= keep_entries:
        return
    overflow = int(count) - keep_entries
    con.execute(
        """
        DELETE FROM replay_frame_cache
        WHERE (replay_id, sample_hz, max_frames) IN (
            SELECT replay_id, sample_hz, max_frames
            FROM replay_frame_cache
            ORDER BY created_at ASC NULLS FIRST
            LIMIT ?
        )
        """,
        [overflow],
    )


def load_replay_frames(
    replay_id: str,
    *,
    hz: int = 8,
    max_frames: int = 1400,
    start_frame: int = 0,
    raw_db: Path | None = None,
    serving_db: Path | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    raw_db = Path(raw_db or settings.raw_db)
    serving_db = Path(serving_db or settings.serving_db)
    stride = max(1, round(60 / max(1, hz)))
    start_frame = max(0, int(start_frame))
    use_cache = start_frame == 0

    if use_cache and serving_db.exists():
        with database_connection(serving_db, read_only=True) as cache_con:
            tables = {
                row[0]
                for row in cache_con.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'main'
                    """
                ).fetchall()
            }
            if "replay_frame_cache" in tables:
                cached = cache_con.execute(
                    """
                    SELECT payload_json
                    FROM replay_frame_cache
                    WHERE replay_id = ? AND sample_hz = ? AND max_frames = ?
                    """,
                    [replay_id, hz, max_frames],
                ).fetchone()
                if cached and cached[0]:
                    payload = json.loads(cached[0])
                    if (
                        payload.get("payload_version") == FRAME_PAYLOAD_VERSION
                        and not _payload_has_unresolved_names(payload)
                        and not _payload_needs_parsed_refresh(cache_con, replay_id, payload)
                    ):
                        return payload

    _ensure_replay_frame_analysis(replay_id, serving_db)

    label_map: dict[str, str] = {}
    player_profiles: dict[str, dict[str, Any]] = {}
    team_name_map: dict[int, str] = {0: "Blue", 1: "Orange"}
    parsed_ready = False
    if serving_db.exists():
        with database_connection(serving_db, read_only=True) as serving:
            tables = {
                row[0]
                for row in serving.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'main'
                    """
                ).fetchall()
            }
            if "remote_players" in tables:
                for player_id, player_name, side, car_name in serving.execute(
                    """
                    SELECT platform_player_id, player_name, side, car_name
                    FROM remote_players
                    WHERE replay_id = ?
                    """,
                    [replay_id],
                ).fetchall():
                    if player_id and player_name:
                        label_map[str(player_id)] = player_name
                        player_profiles[str(player_id)] = {
                            "player_name": player_name,
                            **car_profile(car_name=car_name),
                        }
                    if side == "blue":
                        team_name_map[0] = "Blue"
                    elif side == "orange":
                        team_name_map[1] = "Orange"
            if "remote_replays" in tables:
                try:
                    row = serving.execute(
                        """
                        SELECT blue_team_name, orange_team_name
                        FROM remote_replays
                        WHERE replay_id = ?
                        """,
                        [replay_id],
                    ).fetchone()
                except duckdb.Error:
                    row = None
                if row:
                    if row[0]:
                        team_name_map[0] = row[0]
                    if row[1]:
                        team_name_map[1] = row[1]
            if "replay_parsed_status" in tables:
                row = serving.execute(
                    """
                    SELECT blue_team_name, orange_team_name
                    FROM replay_parsed_status
                    WHERE replay_id = ? AND status = 'completed'
                    """,
                    [replay_id],
                ).fetchone()
                if row:
                    parsed_ready = True
                    if row[0]:
                        team_name_map[0] = row[0]
                    if row[1]:
                        team_name_map[1] = row[1]
            if "events" in tables:
                for player_id, player_name in serving.execute(
                    """
                    SELECT DISTINCT player_id, player_name
                    FROM events
                    WHERE replay_id = ? AND player_id IS NOT NULL
                    """,
                    [replay_id],
                ).fetchall():
                    if player_id and player_name:
                        label_map[str(player_id)] = player_name
            if "replay_parsed_events" in tables:
                for player_id, player_name in serving.execute(
                    """
                    SELECT DISTINCT player_id, player_name
                    FROM replay_parsed_events
                    WHERE replay_id = ? AND player_id IS NOT NULL AND player_name IS NOT NULL
                    """,
                    [replay_id],
                ).fetchall():
                    if player_id and player_name:
                        label_map[str(player_id)] = player_name

    if parsed_ready:
        payload = load_parsed_replay_frames(
            replay_id,
            hz=hz,
            max_frames=max_frames,
            start_frame=start_frame,
            serving_db=serving_db,
        )
        payload = _attach_remote_player_profiles(payload, serving_db, replay_id)
        if use_cache:
            _cache_payload(serving_db, replay_id, hz, max_frames, payload)
        return payload

    rows = []
    if raw_db.exists():
        con = duckdb.connect(str(raw_db), read_only=True)
        try:
            rows = con.execute(
                """
                SELECT bucket, t_approx, payload_zlib
                FROM frames_state
                WHERE replay_id = ? AND bucket % ? = 0
                ORDER BY bucket
                LIMIT ?
                OFFSET ?
                """,
                [replay_id, stride, max_frames, start_frame],
            ).fetchall()
        finally:
            con.close()

    if not rows:
        payload = load_parsed_replay_frames(
            replay_id,
            hz=hz,
            max_frames=max_frames,
            start_frame=start_frame,
            serving_db=serving_db,
        )
        payload = _attach_remote_player_profiles(payload, serving_db, replay_id)
        if use_cache:
            _cache_payload(serving_db, replay_id, hz, max_frames, payload)
        return payload

    if serving_db.exists():
        unresolved_ids = {
            str(player_id)
            for _, _, payload in rows
            for player_id in (json.loads(zlib.decompress(payload).decode("utf-8")).get("cars") or {}).keys()
            if str(player_id) not in label_map
        }
        missing_car_profiles = {
            str(player_id)
            for _, _, payload in rows
            for player_id in (json.loads(zlib.decompress(payload).decode("utf-8")).get("cars") or {}).keys()
            if str(player_id) not in player_profiles
            or not player_profiles[str(player_id)].get("car_name")
        }
        if unresolved_ids or missing_car_profiles:
            for player_id, profile in _infer_ballchasing_player_profiles(serving_db, replay_id, rows).items():
                player_profiles[player_id] = profile
                if profile.get("player_name"):
                    label_map[player_id] = str(profile["player_name"])

    frames: list[dict[str, Any]] = []
    players: dict[str, dict[str, Any]] = {}
    bounds = {
        "min_x": math.inf,
        "max_x": -math.inf,
        "min_y": math.inf,
        "max_y": -math.inf,
        "min_z": math.inf,
        "max_z": -math.inf,
    }
    last_ball = {"pos": [0.0, 0.0, 0.0], "vel": [0.0, 0.0, 0.0]}

    for bucket, t_approx, payload in rows:
        decoded = json.loads(zlib.decompress(payload).decode("utf-8"))
        ball_phys = (decoded.get("ball") or {}).get("phys") or {}
        ball_pos = _vector(ball_phys.get("pos"), last_ball["pos"])
        ball_vel = _vector(ball_phys.get("vel"), last_ball["vel"])
        last_ball = {"pos": ball_pos, "vel": ball_vel}
        frame_cars = []
        for player_id, car in ((decoded.get("cars") or {}).items()):
            phys = car.get("phys") or {}
            pos = _vector(phys.get("pos"), [0.0, 0.0, 17.0])
            vel = _vector(phys.get("vel"), [0.0, 0.0, 0.0])
            euler = _vector(phys.get("euler"), [0.0, 0.0, 0.0])
            team = int(car.get("team") or 0)
            profile = player_profiles.get(str(player_id), {})
            frame_cars.append(
                {
                    "player_id": str(player_id),
                    "player_name": profile.get("player_name") or label_map.get(str(player_id), str(player_id)),
                    "team": team,
                    "team_name": team_name_map.get(team, f"Team {team}"),
                    "car_body_id": profile.get("car_body_id"),
                    "car_name": profile.get("car_name"),
                    "car_family": profile.get("car_family"),
                    "boost": round(float(car.get("boost") or 0.0), 2),
                    "demo": bool(car.get("demo")),
                    "on_ground": bool(car.get("on_ground")),
                    "has_flip": bool(car.get("has_flip")),
                    "pos": pos,
                    "vel": vel,
                    "euler": euler,
                }
            )
            players.setdefault(
                str(player_id),
                {
                    "player_id": str(player_id),
                    "player_name": profile.get("player_name") or label_map.get(str(player_id), str(player_id)),
                    "team": team,
                    "team_name": team_name_map.get(team, f"Team {team}"),
                    "car_body_id": profile.get("car_body_id"),
                    "car_name": profile.get("car_name"),
                    "car_family": profile.get("car_family"),
                },
            )
            _expand_bounds(bounds, pos)
        _expand_bounds(bounds, ball_pos)
        frames.append(
            {
                "bucket": int(bucket),
                "t": round(float(t_approx), 4),
                "ball": {"pos": ball_pos, "vel": ball_vel},
                "cars": frame_cars,
            }
        )

    payload = {
        "payload_version": FRAME_PAYLOAD_VERSION,
        "replay_id": replay_id,
        "source": "raw_frame_store",
        "base_hz": 60,
        "sample_hz": hz,
        "sample_stride": stride,
        "start_frame": start_frame,
        "frame_count": len(frames),
        "total_frame_count": _count_raw_sample_frames(raw_db, replay_id, stride),
        "bounds": {key: 0.0 if math.isinf(value) else round(value, 2) for key, value in bounds.items()},
        "boost_pad_layout": BOOST_PAD_LAYOUT,
        "players": list(players.values()),
        "frames": frames,
    }
    if use_cache:
        _cache_payload(serving_db, replay_id, hz, max_frames, payload)
    return payload


def _ensure_replay_frame_analysis(replay_id: str, serving_db: Path) -> None:
    if not serving_db.exists():
        return
    remote_source = None
    local_file_path = None
    parsed_ready = False
    try:
        with database_connection(serving_db, read_only=True) as con:
            tables = {
                row[0]
                for row in con.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'main'
                    """
                ).fetchall()
            }
            if "replay_parsed_status" in tables:
                row = con.execute(
                    """
                    SELECT status, local_file_path
                    FROM replay_parsed_status
                    WHERE replay_id = ?
                    """,
                    [replay_id],
                ).fetchone()
                if row:
                    parsed_ready = row[0] == "completed"
                    local_file_path = row[1] or local_file_path
            if "remote_replays" in tables:
                try:
                    row = con.execute(
                        """
                        SELECT source, local_file_path
                        FROM remote_replays
                        WHERE replay_id = ?
                        """,
                        [replay_id],
                    ).fetchone()
                except duckdb.Error:
                    row = None
                if row:
                    remote_source = row[0]
                    local_file_path = row[1] or local_file_path
    except (duckdb.Error, FileNotFoundError):
        return

    if parsed_ready:
        return
    try:
        if local_file_path and Path(local_file_path).exists():
            from .carball_ingest import ensure_replay_analysis

            ensure_replay_analysis(replay_id, local_file_path=local_file_path, serving_db=serving_db)
        elif remote_source == "ballchasing":
            ensure_ballchasing_replay_download(replay_id, serving_db=serving_db, parse_download=True)
    except Exception:
        return


def _cache_payload(serving_db: Path, replay_id: str, hz: int, max_frames: int, payload: dict[str, Any]) -> None:
    if not serving_db.exists():
        return
    try:
        cache_con = duckdb.connect(str(serving_db))
    except duckdb.Error:
        return
    try:
        ensure_frame_cache_schema(cache_con)
        cache_con.execute(
            "INSERT OR REPLACE INTO replay_frame_cache VALUES (?, ?, ?, ?, ?, now())",
            [replay_id, hz, max_frames, int(payload.get("frame_count") or 0), json.dumps(payload)],
        )
        prune_frame_cache(cache_con)
    except duckdb.Error:
        return
    finally:
        cache_con.close()


def _count_raw_sample_frames(raw_db: Path, replay_id: str, stride: int) -> int:
    if not raw_db.exists():
        return 0
    try:
        con = duckdb.connect(str(raw_db), read_only=True)
    except duckdb.Error:
        return 0
    try:
        row = con.execute(
            """
            SELECT COUNT(*)
            FROM frames_state
            WHERE replay_id = ? AND bucket % ? = 0
            """,
            [replay_id, stride],
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except duckdb.Error:
        return 0
    finally:
        con.close()


def _payload_has_unresolved_names(payload: dict[str, Any]) -> bool:
    for player in payload.get("players") or []:
        player_id = str(player.get("player_id") or "").strip()
        player_name = str(player.get("player_name") or "").strip()
        if player_id and player_name and player_id == player_name:
            return True
    return False


def _payload_needs_parsed_refresh(
    con: duckdb.DuckDBPyConnection,
    replay_id: str,
    payload: dict[str, Any],
) -> bool:
    if payload.get("source") == "carball":
        return False
    try:
        row = con.execute(
            """
            SELECT status
            FROM replay_parsed_status
            WHERE replay_id = ?
            """,
            [replay_id],
        ).fetchone()
    except duckdb.Error:
        return False
    return bool(row and row[0] == "completed")


def _attach_remote_player_profiles(
    payload: dict[str, Any],
    serving_db: Path,
    replay_id: str,
) -> dict[str, Any]:
    if not serving_db.exists():
        payload["payload_version"] = FRAME_PAYLOAD_VERSION
        return payload
    try:
        with database_connection(serving_db, read_only=True) as con:
            tables = {
                row[0]
                for row in con.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'main'
                    """
                ).fetchall()
            }
            if "remote_players" not in tables:
                payload["payload_version"] = FRAME_PAYLOAD_VERSION
                return payload
            rows = con.execute(
                """
                SELECT platform_player_id, player_name, car_name
                FROM remote_players
                WHERE replay_id = ?
                """,
                [replay_id],
            ).fetchall()
    except (duckdb.Error, FileNotFoundError):
        payload["payload_version"] = FRAME_PAYLOAD_VERSION
        return payload
    if not rows:
        payload["payload_version"] = FRAME_PAYLOAD_VERSION
        return payload

    by_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for player_id, player_name, remote_car_name in rows:
        profile = {"player_name": player_name, **car_profile(car_name=remote_car_name)}
        if player_id:
            by_id[str(player_id)] = profile
        if player_name:
            by_name[str(player_name).casefold()] = profile

    for player in payload.get("players") or []:
        key = str(player.get("player_id") or "")
        name_key = str(player.get("player_name") or "").casefold()
        profile = by_id.get(key) or by_name.get(name_key)
        if profile:
            player.setdefault("car_body_id", profile.get("car_body_id"))
            player["car_name"] = player.get("car_name") or profile.get("car_name")
            player["car_family"] = player.get("car_family") or profile.get("car_family")
    for frame in payload.get("frames") or []:
        for car in frame.get("cars") or []:
            key = str(car.get("player_id") or "")
            name_key = str(car.get("player_name") or "").casefold()
            profile = by_id.get(key) or by_name.get(name_key)
            if profile:
                car.setdefault("car_body_id", profile.get("car_body_id"))
                car["car_name"] = car.get("car_name") or profile.get("car_name")
                car["car_family"] = car.get("car_family") or profile.get("car_family")
    payload["payload_version"] = FRAME_PAYLOAD_VERSION
    return payload


def _infer_ballchasing_player_profiles(
    serving_db: Path,
    replay_id: str,
    rows: list[tuple[Any, Any, bytes]],
) -> dict[str, dict[str, Any]]:
    try:
        with database_connection(serving_db, read_only=True) as con:
            tables = {
                row[0]
                for row in con.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'main'
                    """
                ).fetchall()
            }
            if "remote_replays" not in tables:
                return {}
            row = con.execute(
                """
                SELECT raw_json
                FROM remote_replays
                WHERE replay_id = ?
                """,
                [replay_id],
            ).fetchone()
    except (duckdb.Error, FileNotFoundError):
        return {}
    if not row or not row[0]:
        return {}
    try:
        detail = json.loads(row[0])
    except json.JSONDecodeError:
        return {}

    rosters = {
        0: _remote_roster_stats((detail.get("blue") or {}).get("players") or []),
        1: _remote_roster_stats((detail.get("orange") or {}).get("players") or []),
    }
    raw_stats = _raw_frame_player_stats(rows)
    inferred: dict[str, dict[str, Any]] = {}
    for team, remote_players in rosters.items():
        raw_players = raw_stats.get(team) or []
        if not raw_players or len(raw_players) != len(remote_players):
            continue
        assignment = _best_roster_assignment(raw_players, remote_players)
        for player_id, remote_player in assignment.items():
            inferred[player_id] = {
                "player_name": str(remote_player.get("player_name") or player_id),
                **car_profile(car_name=remote_player.get("car_name")),
            }
    return inferred


def _remote_roster_stats(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for player in players:
        stats = player.get("stats") or {}
        movement = stats.get("movement") or {}
        positioning = stats.get("positioning") or {}
        rows.append(
            {
                "player_name": player.get("name"),
                "car_name": player.get("car_name"),
                "avg_speed": _num(movement.get("avg_speed")),
                "percent_behind_ball": _num(positioning.get("percent_behind_ball")),
                "avg_distance_to_ball": _num(positioning.get("avg_distance_to_ball")),
                "percent_most_back": _num(positioning.get("percent_most_back")),
                "percent_most_forward": _num(positioning.get("percent_most_forward")),
                "percent_closest_to_ball": _num(positioning.get("percent_closest_to_ball")),
                "percent_ground": _num(movement.get("percent_ground")),
            }
        )
    return [row for row in rows if row.get("player_name")]


def _raw_frame_player_stats(rows: list[tuple[Any, Any, bytes]]) -> dict[int, list[dict[str, Any]]]:
    players: dict[str, dict[str, Any]] = {}
    team_signs: dict[int, float] = {}
    for _, _, payload in rows:
        decoded = json.loads(zlib.decompress(payload).decode("utf-8"))
        ball = ((decoded.get("ball") or {}).get("phys") or {}).get("pos") or [0.0, 0.0, 0.0]
        ball_pos = [float(ball[0] or 0.0), float(ball[1] or 0.0), float(ball[2] or 0.0)]
        frame_groups: dict[int, list[dict[str, Any]]] = {}
        for player_id, car in ((decoded.get("cars") or {}).items()):
            phys = car.get("phys") or {}
            pos = phys.get("pos") or [0.0, 0.0, 17.0]
            vel = phys.get("vel") or [0.0, 0.0, 0.0]
            team = int(car.get("team") or 0)
            pos_x = float(pos[0] or 0.0)
            pos_y = float(pos[1] or 0.0)
            pos_z = float(pos[2] or 0.0)
            speed = math.sqrt(sum(float(value or 0.0) ** 2 for value in vel))
            dist_ball = math.sqrt(
                (pos_x - ball_pos[0]) ** 2
                + (pos_y - ball_pos[1]) ** 2
                + (pos_z - ball_pos[2]) ** 2
            )
            bucket = players.setdefault(
                str(player_id),
                {
                    "player_id": str(player_id),
                    "team": team,
                    "avg_y_sum": 0.0,
                    "frames": 0,
                    "speed_sum": 0.0,
                    "distance_sum": 0.0,
                    "ground_frames": 0,
                    "behind_frames": 0,
                    "most_back_frames": 0,
                    "most_forward_frames": 0,
                    "closest_frames": 0,
                },
            )
            bucket["avg_y_sum"] += pos_y
            bucket["frames"] += 1
            bucket["speed_sum"] += speed
            bucket["distance_sum"] += dist_ball
            if car.get("on_ground"):
                bucket["ground_frames"] += 1
            frame_groups.setdefault(team, []).append(
                {
                    "player_id": str(player_id),
                    "y": pos_y,
                    "distance": dist_ball,
                }
            )
        for team, cars in frame_groups.items():
            if team not in team_signs:
                average_y = sum(item["y"] for item in cars) / max(len(cars), 1)
                team_signs[team] = 1.0 if average_y >= 0 else -1.0
            sign = team_signs[team]
            behind = [item for item in cars if (item["y"] - ball_pos[1]) * sign >= 0]
            for item in behind:
                players[item["player_id"]]["behind_frames"] += 1
            backmost = max(cars, key=lambda item: item["y"] * sign)
            forward = min(cars, key=lambda item: item["y"] * sign)
            closest = min(cars, key=lambda item: item["distance"])
            players[backmost["player_id"]]["most_back_frames"] += 1
            players[forward["player_id"]]["most_forward_frames"] += 1
            players[closest["player_id"]]["closest_frames"] += 1

    grouped: dict[int, list[dict[str, Any]]] = {}
    for player in players.values():
        frames = max(int(player["frames"]), 1)
        grouped.setdefault(int(player["team"]), []).append(
            {
                "player_id": player["player_id"],
                "avg_speed": player["speed_sum"] / frames,
                "avg_distance_to_ball": player["distance_sum"] / frames,
                "percent_ground": player["ground_frames"] * 100.0 / frames,
                "percent_behind_ball": player["behind_frames"] * 100.0 / frames,
                "percent_most_back": player["most_back_frames"] * 100.0 / frames,
                "percent_most_forward": player["most_forward_frames"] * 100.0 / frames,
                "percent_closest_to_ball": player["closest_frames"] * 100.0 / frames,
            }
        )
    return grouped


def _best_roster_assignment(
    raw_players: list[dict[str, Any]],
    remote_players: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not raw_players or len(raw_players) != len(remote_players):
        return {}
    best_cost = math.inf
    best_mapping: dict[str, dict[str, Any]] = {}
    for ordered in permutations(remote_players):
        mapping: dict[str, dict[str, Any]] = {}
        cost = 0.0
        for raw_player, remote_player in zip(raw_players, ordered, strict=False):
            mapping[raw_player["player_id"]] = remote_player
            cost += _roster_match_cost(raw_player, remote_player)
        if cost < best_cost:
            best_cost = cost
            best_mapping = mapping
    return best_mapping


def _roster_match_cost(raw_player: dict[str, Any], remote_player: dict[str, Any]) -> float:
    return (
        _scaled_distance(raw_player.get("avg_speed"), remote_player.get("avg_speed"), 220.0, weight=2.0)
        + _scaled_distance(raw_player.get("percent_behind_ball"), remote_player.get("percent_behind_ball"), 16.0, weight=1.5)
        + _scaled_distance(raw_player.get("avg_distance_to_ball"), remote_player.get("avg_distance_to_ball"), 420.0, weight=1.5)
        + _scaled_distance(raw_player.get("percent_most_back"), remote_player.get("percent_most_back"), 14.0, weight=1.0)
        + _scaled_distance(raw_player.get("percent_most_forward"), remote_player.get("percent_most_forward"), 14.0, weight=1.0)
        + _scaled_distance(raw_player.get("percent_closest_to_ball"), remote_player.get("percent_closest_to_ball"), 18.0, weight=1.0)
        + _scaled_distance(raw_player.get("percent_ground"), remote_player.get("percent_ground"), 18.0, weight=0.6)
    )


def _scaled_distance(left: Any, right: Any, scale: float, *, weight: float) -> float:
    left_value = _num(left)
    right_value = _num(right)
    if left_value is None or right_value is None:
        return 0.0
    return abs(left_value - right_value) / max(scale, 1.0) * weight


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _vector(value: Any, fallback: list[float]) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        return [round(float(item), 4) for item in fallback]
    return [round(float(item), 4) for item in value]


def _expand_bounds(bounds: dict[str, float], pos: list[float]) -> None:
    bounds["min_x"] = min(bounds["min_x"], pos[0])
    bounds["max_x"] = max(bounds["max_x"], pos[0])
    bounds["min_y"] = min(bounds["min_y"], pos[1])
    bounds["max_y"] = max(bounds["max_y"], pos[1])
    bounds["min_z"] = min(bounds["min_z"], pos[2])
    bounds["max_z"] = max(bounds["max_z"], pos[2])
