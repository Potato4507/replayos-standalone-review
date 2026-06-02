# ReplayOS Review Platform

This repository is the standalone replay-review version of ReplayOS.

It is built around one job:

- pull Rocket League replays from Ballchasing
- parse them into local 60 Hz telemetry
- keep a replay shelf
- preserve replay semantics, team recognition, player naming, and series grouping
- open each replay in a review webpage with a native 3D viewer, swing tracking, turning points, and player impact

There is no required GPT usage and no extra third-party media service in the standalone release.
This release is the review core only. It does not ship the wider records, live, leaderboard, or pro-site surfaces.

## Run It

```powershell
cd D:\RocketLeagueFrames
python -m pip install -e .[dev]
python standalone_replay_review.py
```

Open:

```text
http://127.0.0.1:8010
```

## What You Get

- a local review webpage
- saved Ballchasing token support
- single replay import by replay id or URL
- Ballchasing group sync
- Ballchasing creator sync
- optional background auto-sync
- a local replay shelf
- semantic replay moments, canonical team naming, and canonical player naming
- review summaries and timeline markers
- a native 60 Hz 3D replay viewer

## Ballchasing Setup

You only need a Ballchasing API token.

You can add sources in the UI as:

- replay URLs or replay ids
- group URLs or group ids
- creator URLs or creator ids

Examples:

```text
https://ballchasing.com/replay/92aa7211-d35d-4b3c-b93f-8a9faf21ac24
https://ballchasing.com/group/ewc-rl-2025-d7nloy3ch5
https://ballchasing.com/groups?creator=76561199225615730
```

## Local Workspace

The standalone app stores its data under:

```text
output/review-platform/
```

That folder contains:

- `review-platform.duckdb`
- `replays/`
- `platform-config.json`

## Main Routes

- `GET /`
- `GET /health`
- `GET /api/status`
- `POST /api/config`
- `POST /api/replays/import`
- `POST /api/sources/sync`
- `GET /api/replays`
- `GET /api/replays/{replay_id}`
- `GET /api/replays/{replay_id}/viewer`
- `GET /api/replays/{replay_id}/native-viewer`
- `GET /api/replays/{replay_id}/file`

## Tests

```powershell
python -m unittest tests.test_standalone_replay_review tests.test_site tests.test_native_viewer
```

## More Detail

See [docs/standalone-replay-review.md](docs/standalone-replay-review.md).
