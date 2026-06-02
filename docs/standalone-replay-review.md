# Standalone Replay Review

## Overview

`standalone_replay_review.py` runs the project as a local replay-review application.

The standalone app gives you:

- a local review webpage
- Ballchasing token storage
- replay import by Ballchasing replay id or URL
- replay sync from Ballchasing groups and creator feeds
- optional background auto-sync
- a replay shelf
- replay semantics, canonical team recognition, canonical player naming, and series grouping
- review summaries, swing markers, player impact, and the native 60 Hz 3D viewer

This app is intentionally narrow. It is for replay review, not the broader records, live, or pro-site feature set.

## Workspace

The standalone app keeps its own local workspace under:

```text
output/review-platform/
```

That workspace contains:

- `review-platform.duckdb`
- `replays/`
- `platform-config.json`

Nothing else is required to use the local review flow.

## Quick Start

From the project root:

```powershell
cd D:\RocketLeagueFrames
python -m pip install -e .[dev]
python standalone_replay_review.py
```

Open:

```text
http://127.0.0.1:8010
```

Then:

1. Save your Ballchasing API token.
2. Import one replay by replay id or replay URL, or add source feeds.
3. Open any replay from the shelf.

## Ballchasing Sources

The standalone app accepts:

- Ballchasing replay URLs or replay ids for one-off imports
- Ballchasing group ids or group URLs
- Ballchasing creator ids or creator URLs

Typical source examples:

```text
https://ballchasing.com/replay/92aa7211-d35d-4b3c-b93f-8a9faf21ac24
https://ballchasing.com/group/ewc-rl-2025-d7nloy3ch5
https://ballchasing.com/groups?creator=76561199225615730
```

## Auto-Sync

If auto-sync is enabled in the UI, the app runs a small local worker in-process.

That worker:

- reads the saved Ballchasing sources
- downloads new replays
- parses them locally
- refreshes the replay shelf

This is meant for a local workstation. It is not set up as a hosted multi-user service.

## Routes

- `/`
  Standalone review webpage
- `/health`
  Health check
- `/api/status`
  Workspace counts, config summary, and sync state
- `/api/config`
  Save the Ballchasing token and source settings
- `/api/replays/import`
  Import one replay
- `/api/sources/sync`
  Sync configured Ballchasing sources
- `/api/replays`
  Replay shelf listing
- `/api/replays/{replay_id}`
  Replay metadata
- `/api/replays/{replay_id}/viewer`
  Review payload
- `/api/replays/{replay_id}/native-viewer`
  Native viewer payload
- `/api/replays/{replay_id}/file`
  Local `.replay` file download

## Cost

The standalone review app stays cheap:

- Ballchasing token: required
- extra third-party media APIs: not required
- GPT: not required
- paid live-data APIs: not required because the standalone app does not include live-site features

## Notes

- The Ballchasing token is stored locally in `platform-config.json`.
- Replays are parsed into local 60 Hz telemetry before the richer review layers are shown.
- The embedded 3D viewer uses the same local native-viewer assets already shipped with the project.
