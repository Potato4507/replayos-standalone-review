from __future__ import annotations

import gzip
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from .config import PROJECT_ROOT

VIEWER_PAYLOAD_SCHEMA_VERSION = 3
VIEWER_PAYLOAD_CACHE_VERSION = 3
VIEWER_CACHE_ROOT = PROJECT_ROOT / "cache" / "native-viewer"

DEFAULT_CAMERA_SETTINGS = {
    "distance": 270.0,
    "fieldOfView": 110.0,
    "transitionSpeed": 1.0,
    "pitch": -4.0,
    "swivelSpeed": 4.0,
    "stiffness": 0.45,
    "height": 110.0,
}

DEFAULT_LOADOUT = {
    "antenna": 0,
    "banner": 0,
    "boost": 0,
    "car": 23,
    "engineAudio": 0,
    "goalExplosion": 0,
    "skin": 0,
    "topper": 0,
    "trail": 0,
    "wheels": 0,
    "primaryColor": 67,
    "accentColor": 67,
    "bannerPaint": 0,
    "boostPaint": 0,
    "carPaint": 0,
    "goalExplosionPaint": 0,
    "skinPaint": 0,
    "trailPaint": 0,
    "wheelsPaint": 0,
    "topperPaint": 0,
    "antennaPaint": 0,
}


def build_native_viewer_payload(
    replay: dict[str, Any],
    parsed_payload: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    frames = list(parsed_payload.get("frames") or [])
    players = list(parsed_payload.get("players") or [])
    if not frames or not players:
        raise FileNotFoundError(f"No parsed replay telemetry available for {replay.get('replay_id')}")

    replay_id = str(replay.get("replay_id") or parsed_payload.get("replay_id") or "")
    parsed_replay_id = str(parsed_payload.get("replay_id") or replay_id)
    base_hz = max(1, int(parsed_payload.get("base_hz") or 60))
    sample_hz = max(1, int(parsed_payload.get("sample_hz") or base_hz))
    overtime = bool(replay.get("overtime"))
    team_names = {
        0: str(replay.get("blue_team_name") or "Blue"),
        1: str(replay.get("orange_team_name") or "Orange"),
    }
    remote_players = _remote_player_lookup(replay.get("players") or [])

    ordered_players = _ordered_players(players)
    per_player_frames = {player["player_id"]: [] for player in ordered_players}
    per_player_ball_cam = {player["player_id"]: [] for player in ordered_players}
    per_player_boost = {player["player_id"]: [] for player in ordered_players}
    previous_boost = {
        player["player_id"]: float(_frame_car(frames[0], player["player_id"]).get("boost") or 0.0)
        for player in ordered_players
    }
    replay_frames: list[list[float | int]] = []
    ball_frames: list[list[float]] = []
    pad_state_masks: list[int] = []
    last_time = 0.0

    for index, frame in enumerate(frames):
        t_value = float(frame.get("t") or 0.0)
        delta = 0.0 if index == 0 else max((1.0 / sample_hz) * 0.5, t_value - last_time)
        game_time = _game_clock_seconds(t_value, overtime=overtime)
        replay_frames.append([round(delta, 6), game_time, round(t_value, 4)])
        last_time = t_value

        ball_data = frame.get("ball") or {}
        ball_pos = ball_data.get("pos") or [0.0, 0.0, 92.75]
        ball_vel = ball_data.get("vel") or [0.0, 0.0, 0.0]
        clean_ball_pos = [_round(ball_pos, 0), _round(ball_pos, 1), _round(ball_pos, 2, 92.75)]
        clean_ball_vel = [_round(ball_vel, 0), _round(ball_vel, 1), _round(ball_vel, 2)]
        ball_frames.append([clean_ball_pos[0], clean_ball_pos[1], clean_ball_pos[2], 0.0, 0.0, 0.0])
        current_pad_state = [bool(value) for value in (frame.get("pad_states") or [])]
        pad_state_masks.append(_pad_state_mask(current_pad_state))

        cars_by_id = {str(car.get("player_id") or ""): car for car in (frame.get("cars") or [])}
        for player in ordered_players:
            player_id = player["player_id"]
            car = cars_by_id.get(player_id) or _fallback_car_state(player, team_names)
            pos = car.get("pos") or [0.0, 0.0, 17.0]
            vel = car.get("vel") or [0.0, 0.0, 0.0]
            euler = car.get("euler") or [0.0, 0.0, 0.0]
            boost_value = round(float(car.get("boost") or 0.0), 2)
            boost_active = _bool_or_none(car.get("boost_active"))
            if boost_active is None:
                boost_active = boost_value < previous_boost[player_id] - 0.2
            previous_boost[player_id] = boost_value
            ball_cam = _bool_or_none(car.get("ball_cam", car.get("ballCam")))
            per_player_ball_cam[player_id].append(ball_cam)
            per_player_boost[player_id].append(boost_value)
            per_player_frames[player_id].append([
                _round(pos, 0),
                _round(pos, 1),
                _round(pos, 2, 17.0),
                _round(euler, 0, precision=6),
                _round(euler, 1, precision=6),
                _round(euler, 2, precision=6),
                bool(boost_active),
                boost_value,
            ])

    metadata_players = []
    names: list[str] = []
    colors: list[bool] = []
    player_cards: list[dict[str, Any]] = []
    for player in ordered_players:
        player_id = str(player.get("player_id") or "")
        player_name = str(player.get("player_name") or player_id)
        team = int(player.get("team") or 0)
        remote = remote_players.get((team, _identity_key(player_name))) or {}
        camera_settings = _player_camera_settings(player)
        loadout = _player_loadout(player)
        names.append(player_name)
        colors.append(team == 1)
        metadata_players.append(
            {
                "id": {"id": player_id},
                "name": player_name,
                "isOrange": team == 1,
                "score": int(remote.get("score") or 0),
                "goals": int(remote.get("goals") or 0),
                "assists": int(remote.get("assists") or 0),
                "saves": int(remote.get("saves") or 0),
                "shots": int(remote.get("shots") or 0),
                "cameraSettings": camera_settings,
                "loadout": loadout,
            }
        )
        player_cards.append(
            {
                "player_id": player_id,
                "player_name": player_name,
                "team": team,
                "team_name": team_names.get(team, "Team"),
                "car_name": player.get("car_name") or remote.get("car_name") or "Octane",
                "car_family": player.get("car_family"),
                "car_body_id": player.get("car_body_id"),
                "camera_settings": camera_settings,
                "loadout": loadout,
            }
        )

    goals = _goal_frames(events, base_hz=sample_hz, frame_count=len(replay_frames))
    director_hints = _director_hints(events, base_hz=sample_hz, frame_count=len(replay_frames))
    boost_pad_layout = parsed_payload.get("boost_pad_layout") or []

    payload = {
        "payload_schema": VIEWER_PAYLOAD_SCHEMA_VERSION,
        "replay_id": replay_id,
        "request": {
            "replay_id": replay_id,
            "parsed_replay_id": parsed_replay_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sample_hz": sample_hz,
            "base_hz": base_hz,
            "frame_count": len(replay_frames),
            "source": parsed_payload.get("source") or "carball",
            "cache_stale": bool(parsed_payload.get("cache_stale")),
            "cache_warning": parsed_payload.get("cache_warning"),
        },
        "replay": {
            "replay_id": replay_id,
            "title": replay.get("title") or f"{team_names[0]} vs {team_names[1]}",
            "blue_team_name": team_names[0],
            "orange_team_name": team_names[1],
            "blue_goals": int(replay.get("blue_goals") or 0),
            "orange_goals": int(replay.get("orange_goals") or 0),
            "map_code": replay.get("map_code") or "TrainStation_Night_P",
            "overtime": overtime,
        },
        "replayData": {
            "id": replay_id,
            "names": names,
            "colors": colors,
            "frames": replay_frames,
            "ball": ball_frames,
            "players": [per_player_frames[player["player_id"]] for player in ordered_players],
        },
        "nativeTelemetry": {
            "schema_version": VIEWER_PAYLOAD_SCHEMA_VERSION,
            "replay_id": replay_id,
            "base_hz": base_hz,
            "sample_hz": sample_hz,
            "sample_stride": parsed_payload.get("sample_stride"),
            "start_frame": parsed_payload.get("start_frame", 0),
            "frame_count": len(replay_frames),
            "total_frame_count": parsed_payload.get("total_frame_count"),
            "bounds": parsed_payload.get("bounds") or {},
            "player_order": [player["player_id"] for player in ordered_players],
            "director_hints": director_hints,
            "boost_pad_layout": boost_pad_layout,
        },
        "director_hints": director_hints,
        "replayMetadata": {
            "version": 1,
            "teams": [
                {"name": team_names[0], "isOrange": False},
                {"name": team_names[1], "isOrange": True},
            ],
            "gameStats": {},
            "gameMetadata": {
                "id": replay_id,
                "name": replay.get("title") or f"{team_names[0]} vs {team_names[1]}",
                "map": replay.get("map_code") or "TrainStation_Night_P",
                "version": 1,
                "time": str(_epoch_seconds(replay.get("match_date") or replay.get("ingested_at"))),
                "frames": len(replay_frames),
                "score": {
                    "team0Score": int(replay.get("blue_goals") or 0),
                    "team1Score": int(replay.get("orange_goals") or 0),
                },
                "goals": goals,
            },
            "players": metadata_players,
        },
        "hud": {
            "player_order": [player["player_id"] for player in ordered_players],
            "player_cards": player_cards,
            "ball_cam_by_player": per_player_ball_cam,
            "boost_by_player": per_player_boost,
            "pad_state_masks": pad_state_masks,
            "boost_pad_layout": boost_pad_layout,
        },
    }
    return payload


def _safe_cache_part(value: Any) -> str:
    raw = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)
    return safe[:160] or "unknown"


def native_viewer_cache_path(
    replay_id: str,
    *,
    hz: int,
    max_frames: int,
    start_frame: int,
    parser_version: str,
) -> Path:
    version_dir = f"v{VIEWER_PAYLOAD_CACHE_VERSION}-schema{VIEWER_PAYLOAD_SCHEMA_VERSION}-{_safe_cache_part(parser_version)}"
    file_name = f"h{int(hz)}-m{int(max_frames)}-s{int(start_frame)}.json.gz"
    return VIEWER_CACHE_ROOT / version_dir / _safe_cache_part(replay_id) / file_name


def load_native_viewer_payload_cache(
    replay_id: str,
    *,
    hz: int,
    max_frames: int,
    start_frame: int,
    parser_version: str,
) -> bytes | None:
    cache_path = native_viewer_cache_path(
        replay_id,
        hz=hz,
        max_frames=max_frames,
        start_frame=start_frame,
        parser_version=parser_version,
    )
    try:
        if cache_path.exists():
            return cache_path.read_bytes()
    except OSError:
        return None
    return None


def encode_native_viewer_payload(payload: dict[str, Any]) -> bytes:
    json_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return gzip.compress(json_bytes, compresslevel=5)


def store_native_viewer_payload_cache(
    replay_id: str,
    payload: dict[str, Any],
    *,
    hz: int,
    max_frames: int,
    start_frame: int,
    parser_version: str,
) -> bytes:
    encoded = encode_native_viewer_payload(payload)
    cache_path = native_viewer_cache_path(
        replay_id,
        hz=hz,
        max_frames=max_frames,
        start_frame=start_frame,
        parser_version=parser_version,
    )
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cache_path.with_suffix(f"{cache_path.suffix}.tmp")
        temp_path.write_bytes(encoded)
        temp_path.replace(cache_path)
    except OSError:
        pass
    return encoded


def native_viewer_gzip_response(encoded_payload: bytes, request: Request) -> Response:
    headers = {"Vary": "Accept-Encoding"}
    accepts_gzip = "gzip" in str(request.headers.get("accept-encoding") or "").lower()
    if accepts_gzip:
        headers["Content-Encoding"] = "gzip"
        return Response(content=encoded_payload, media_type="application/json", headers=headers)
    return Response(content=gzip.decompress(encoded_payload), media_type="application/json", headers=headers)


def _pad_state_mask(values: list[bool]) -> int:
    mask = 0
    for index, active in enumerate(values[:52]):
        if active:
            mask += 2 ** index
    return mask


def _round(values: Any, index: int, default: float = 0.0, precision: int = 3) -> float:
    try:
        value = float(values[index])
    except (IndexError, TypeError, ValueError):
        value = default
    return round(value, precision)


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on", "ball"}:
            return True
        if normalized in {"false", "0", "no", "off", "car"}:
            return False
    return None


def _ordered_players(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "player_id": str(player.get("player_id") or ""),
                "player_name": str(player.get("player_name") or player.get("player_id") or "Unknown"),
                "team": int(player.get("team") or 0),
                "car_name": player.get("car_name"),
                "car_family": player.get("car_family"),
                "car_body_id": player.get("car_body_id"),
                "camera_settings": player.get("camera_settings"),
                "loadout": player.get("loadout"),
            }
            for player in players
        ],
        key=lambda item: (item["team"], item["player_name"].casefold(), item["player_id"]),
    )


def _frame_car(frame: dict[str, Any], player_id: str) -> dict[str, Any]:
    for car in frame.get("cars") or []:
        if str(car.get("player_id") or "") == player_id:
            return car
    return {}


def _fallback_car_state(player: dict[str, Any], team_names: dict[int, str]) -> dict[str, Any]:
    team = int(player.get("team") or 0)
    return {
        "player_id": player["player_id"],
        "player_name": player["player_name"],
        "team": team,
        "team_name": team_names.get(team, "Team"),
        "boost": 0.0,
        "pos": [0.0, 0.0, 17.0],
        "vel": [0.0, 0.0, 0.0],
        "euler": [0.0, 0.0, 0.0],
    }


def _remote_player_lookup(rows: list[dict[str, Any]]) -> dict[tuple[int, str], dict[str, Any]]:
    lookup: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        side = str(row.get("side") or "").casefold()
        team = 1 if side == "orange" else 0
        name = str(row.get("player_name") or "").strip()
        if not name:
            continue
        lookup[(team, _identity_key(name))] = row
    return lookup


def _identity_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _game_clock_seconds(elapsed_seconds: float, *, overtime: bool) -> int:
    if overtime:
        return max(0, int(round(300 - min(elapsed_seconds, 300))))
    return max(0, int(round(300 - elapsed_seconds)))


def _epoch_seconds(value: Any) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    return 0


def _event_frame(event: dict[str, Any], *, base_hz: int, frame_count: int) -> int:
    t_value = float(event.get("t") or 0.0)
    return min(max(frame_count - 1, 0), max(0, int(round(t_value * base_hz))))


def _goal_frames(events: list[dict[str, Any]], *, base_hz: int, frame_count: int) -> list[dict[str, Any]]:
    goals = []
    for event in events:
        if str(event.get("event_type") or "") != "goal":
            continue
        player_id = str(event.get("player_id") or "")
        if not player_id:
            continue
        goals.append({"frameNumber": _event_frame(event, base_hz=base_hz, frame_count=frame_count), "playerId": {"id": player_id}})
    return goals


def _director_hints(events: list[dict[str, Any]], *, base_hz: int, frame_count: int) -> list[dict[str, Any]]:
    allowed = {"goal", "demo", "touch", "shot", "save", "pressure_phase", "turnover"}
    hints: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        if event_type not in allowed:
            continue
        frame = _event_frame(event, base_hz=base_hz, frame_count=frame_count)
        weight = {
            "goal": 1.0,
            "shot": 0.82,
            "save": 0.74,
            "demo": 0.48,
            "pressure_phase": 0.52,
            "turnover": 0.5,
            "touch": 0.24,
        }.get(event_type, 0.25)
        label = event_type.replace("_", " ")
        if event.get("player_name"):
            label = f"{event.get('player_name')} {label}"
        hints.append(
            {
                "frame": frame,
                "t": round(float(event.get("t") or 0.0), 4),
                "type": event_type,
                "label": label,
                "weight": weight,
                "player_id": event.get("player_id"),
                "player_name": event.get("player_name"),
                "team": event.get("team_color") or event.get("team_id"),
            }
        )
    hints.sort(key=lambda item: (item["frame"], -float(item["weight"])))
    return hints


def _player_loadout(player: dict[str, Any]) -> dict[str, Any]:
    loadout = dict(DEFAULT_LOADOUT)
    raw_loadout = player.get("loadout")
    if isinstance(raw_loadout, dict):
        for key, value in raw_loadout.items():
            if value is None:
                continue
            loadout[str(key)] = value
    try:
        car_id = int(player.get("car_body_id")) if player.get("car_body_id") is not None else None
    except (TypeError, ValueError):
        car_id = None
    if car_id:
        loadout["car"] = car_id
    return loadout


def _player_camera_settings(player: dict[str, Any]) -> dict[str, Any]:
    camera_settings = dict(DEFAULT_CAMERA_SETTINGS)
    raw_settings = player.get("camera_settings")
    if isinstance(raw_settings, dict):
        for key, value in raw_settings.items():
            if value is None:
                continue
            # Keep both parser snake_case and viewer camelCase when possible.
            normalized_key = {
                "field_of_view": "fieldOfView",
                "transition_speed": "transitionSpeed",
                "swivel_speed": "swivelSpeed",
            }.get(str(key), str(key))
            camera_settings[normalized_key] = value
            camera_settings[str(key)] = value
    return camera_settings
