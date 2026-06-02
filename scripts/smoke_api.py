from __future__ import annotations

import json
import sys
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.parse import quote


def get_json(base_url: str, path: str, *, timeout: int = 15) -> dict:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}{path}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check(base_url: str, path: str, *, timeout: int = 15) -> dict:
    try:
        payload = get_json(base_url, path, timeout=timeout)
        return {
            "path": path,
            "ok": True,
            "keys": list(payload)[:6] if isinstance(payload, dict) else [type(payload).__name__],
            "payload": payload,
        }
    except HTTPError as exc:
        return {"path": path, "ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except URLError as exc:
        return {"path": path, "ok": False, "error": str(exc.reason)}
    except Exception as exc:  # pragma: no cover - smoke script fallback
        return {"path": path, "ok": False, "error": str(exc)}


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    checks = [
        "/health",
        "/summary",
        "/replays?limit=3",
        "/library/replays?limit=5&parsed_only=true&review_ready=true",
        "/teams?limit=4",
        "/players?limit=4",
        "/model-versions",
        "/site/home",
        "/site/live",
        "/site/records",
        "/sources/maintenance/status",
        "/sources/carball/status",
        "/sources/ballchasing/status",
        "/sources/youtube/status",
    ]
    results = [check(base_url, path) for path in checks]

    home_payload = next((item["payload"] for item in results if item["path"] == "/site/home" and item["ok"]), {})
    records_payload = next((item["payload"] for item in results if item["path"] == "/site/records" and item["ok"]), {})
    replays_payload = next((item["payload"] for item in results if item["path"] == "/replays?limit=3" and item["ok"]), {})
    library_payload = next((item["payload"] for item in results if item["path"] == "/library/replays?limit=5&parsed_only=true&review_ready=true" and item["ok"]), {})

    replay_items = library_payload.get("items") or replays_payload.get("items") or []
    replay_id = replay_items[0]["replay_id"] if replay_items else None
    team_name = (records_payload.get("team_options") or [None])[0]
    player_name = (records_payload.get("player_options") or [None])[0]

    if replay_id:
        checks = [
            f"/library/replays/{replay_id}/viewer",
            f"/library/replays/{replay_id}/frames?hz=60&max_frames=180",
            f"/library/replays/{replay_id}/videos",
        ]
        for path in checks:
            results.append(check(base_url, path, timeout=60 if "/frames" in path else 15))

    if team_name:
        encoded_team = quote(team_name)
        results.append(check(base_url, f"/site/records/team?name={encoded_team}"))

    if player_name:
        encoded_player = quote(player_name)
        results.append(check(base_url, f"/site/records/player?name={encoded_player}"))

    failures = [item for item in results if not item["ok"]]
    for item in results:
        if item["ok"]:
            print(f"{item['path']}: ok ({item['keys']})")
        else:
            print(f"{item['path']}: error ({item['error']})")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
