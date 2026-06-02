from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def parse_event_meta(meta: str | None) -> dict[str, Any]:
    if not meta:
        return {}
    try:
        value = json.loads(meta)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def build_touch_chains(events: Iterable[dict[str, Any]], *, gap_seconds: float = 2.5) -> list[dict[str, Any]]:
    touches = sorted(
        (event for event in events if event.get("event_type") == "touch"),
        key=lambda event: float(event.get("t") or 0.0),
    )
    chains: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for event in touches:
        t = float(event.get("t") or 0.0)
        player_id = event.get("player_id")
        team_color = event.get("team_color") or event.get("team")
        should_start = (
            current is None
            or player_id != current["player_id"]
            or team_color != current["team_color"]
            or t - current["end_t"] > gap_seconds
        )
        if should_start:
            if current is not None:
                chains.append(current)
            current = {
                "kind": "touch_chain",
                "start_t": t,
                "end_t": t,
                "duration": 0.0,
                "team_color": team_color,
                "player_id": player_id,
                "player_name": event.get("player_name"),
                "touches": 1,
            }
            continue

        current["end_t"] = t
        current["duration"] = round(current["end_t"] - current["start_t"], 3)
        current["touches"] += 1

    if current is not None:
        chains.append(current)
    return chains


def build_possession_phases(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(events, key=lambda event: float(event.get("t") or 0.0))
    phases: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None

    for event in ordered:
        event_type = event.get("event_type")
        t = float(event.get("t") or 0.0)
        team_color = event.get("team_color") or event.get("team")
        if event_type == "possession_start":
            if active is not None:
                active["end_t"] = t
                active["duration"] = round(active["end_t"] - active["start_t"], 3)
                active["ended_by"] = "turnover"
                phases.append(active)
            active = {
                "kind": "possession",
                "start_t": t,
                "end_t": t,
                "duration": 0.0,
                "team_color": team_color,
                "player_id": event.get("player_id"),
                "player_name": event.get("player_name"),
                "ended_by": None,
            }
        elif event_type == "possession_end" and active is not None:
            active["end_t"] = t
            active["duration"] = round(active["end_t"] - active["start_t"], 3)
            active["ended_by"] = "possession_end"
            phases.append(active)
            active = None
        elif event_type == "goal" and active is not None:
            active["end_t"] = t
            active["duration"] = round(active["end_t"] - active["start_t"], 3)
            active["ended_by"] = "goal"
            phases.append(active)
            active = None
        elif event_type == "loose_ball_start":
            if active is not None:
                active["end_t"] = t
                active["duration"] = round(active["end_t"] - active["start_t"], 3)
                active["ended_by"] = "loose_ball"
                phases.append(active)
                active = None
            phases.append(
                {
                    "kind": "loose_ball",
                    "start_t": t,
                    "end_t": t,
                    "duration": 0.0,
                    "team_color": None,
                    "ended_by": "open_play",
                }
            )

    if active is not None:
        active["duration"] = round(active["end_t"] - active["start_t"], 3)
        active["ended_by"] = "open"
        phases.append(active)
    return phases


def build_turning_points(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(events, key=lambda event: float(event.get("t") or 0.0))
    points: list[dict[str, Any]] = []
    for event in ordered:
        event_type = event.get("event_type")
        if event_type not in {"goal", "demo", "boost_starvation_window"}:
            continue
        t = float(event.get("t") or 0.0)
        points.append(
            {
                "t": t,
                "event_type": event_type,
                "team_color": event.get("team_color") or event.get("team"),
                "player_name": event.get("player_name"),
                "label": _turning_point_label(event),
                "weight": _turning_point_weight(event_type),
            }
        )
    return points


def _turning_point_label(event: dict[str, Any]) -> str:
    event_type = event.get("event_type")
    team = event.get("team_color") or event.get("team")
    player = event.get("player_name") or event.get("player_id") or "Unknown"
    if event_type == "goal":
        return f"{team or 'Unknown'} goal by {player}"
    if event_type == "demo":
        return f"{team or 'Unknown'} demo pressure"
    if event_type == "boost_starvation_window":
        return f"{team or 'Unknown'} boost starvation window"
    return "Replay event"


def _turning_point_weight(event_type: str) -> float:
    weights = {
        "goal": 1.0,
        "demo": 0.55,
        "boost_starvation_window": 0.45,
    }
    return weights.get(event_type, 0.1)


def build_replay_timeline(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    materialized = list(events)
    return {
        "touch_chains": build_touch_chains(materialized),
        "phases": build_possession_phases(materialized),
        "turning_points": build_turning_points(materialized),
    }


def feature_reason_codes(feature_row: dict[str, Any]) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    pairs = [
        ("Boost advantage", feature_row.get("clutch_boost_advantage")),
        ("Pressure rate", feature_row.get("pressure_rate")),
        ("Touch rate", feature_row.get("touch_rate")),
        ("Possession rate", feature_row.get("possession_rate")),
        ("Aerial rate", feature_row.get("aerial_rate")),
        ("Starvation risk", feature_row.get("starvation_rate")),
        ("Overcommit risk", feature_row.get("overcommit_rate")),
    ]
    for name, value in pairs:
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        reasons.append({"name": name, "value": round(numeric, 4), "magnitude": abs(numeric)})
    return sorted(reasons, key=lambda item: item["magnitude"], reverse=True)[:5]
