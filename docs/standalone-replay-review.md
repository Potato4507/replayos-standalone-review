# Review-Only ReplayOS

## What this is

`standalone_replay_review.py` is now a small local review platform, not a one-replay demo.

It gives people:

- a saved Ballchasing API token
- saved Ballchasing creator and group sources
- optional background auto-sync
- a local replay shelf
- the ReplayOS review stack for any replay in that shelf
- the native 60 Hz 3D viewer

This is meant to feel like the ReplayOS review tab pulled out into its own product.

## What it does

The app keeps one shared local workspace under:

```text
output/review-platform/
```

That workspace contains:

- `review-platform.duckdb`
- `replays/`
- `platform-config.json`

From there, the platform can:

1. import one replay by Ballchasing replay id or URL
2. sync whole Ballchasing groups
3. sync creator feeds and expand them into groups
4. parse downloaded replays into local 60 Hz telemetry
5. build review payloads on demand
6. open the native replay viewer from the same local shelf

## Quick start

From the project root:

```powershell
pip install -e .
python standalone_replay_review.py
```

Open:

```text
http://127.0.0.1:8010
```

Then:

1. save your Ballchasing API token
2. add one or more group ids / group URLs or creator ids / creator URLs
3. choose whether auto-sync should run
4. click `Sync sources now`
5. open a replay from the shelf

You can also skip the source setup and just import one replay directly.

## Why this is different from the full site

This platform is intentionally narrower than full ReplayOS:

- no live RLCS pages
- no records dashboard
- no global stats portal
- no analyst desk
- no required GPT usage

It focuses on one thing: replay review.

## What it reuses from the main app

This is still using the real ReplayOS internals:

- Ballchasing sync and download flow from `replayos.ballchasing`
- 60 Hz replay parsing from `replayos.carball_ingest`
- replay shelf queries from `replayos.site`
- review payload generation from `replayos.site`
- native viewer payload generation from `replayos.native_viewer`
- native viewer frontend from `frontend/public/native-viewer`

That keeps the review-only app aligned with the main codebase instead of becoming a fake fork.

## Main routes

- `/`
  Review-only local UI

- `/api/status`
  Workspace counts, source settings summary, and sync state

- `/api/config`
  Save token, group sources, creator sources, and auto-sync settings

- `/api/replays/import`
  Import one replay into the local shelf

- `/api/sources/sync`
  Sync configured creator/group sources

- `/api/replays`
  Shelf listing with search, parsed-only, review-ready, and sort options

- `/api/replays/{replay_id}/viewer`
  Full replay review payload for one replay

- `/api/replays/{replay_id}/native-viewer`
  Native 3D viewer payload

- `/api/replays/{replay_id}/file`
  Download the local replay file

The `/library/...` aliases still exist so the embedded native viewer keeps working cleanly.

## Auto-sync

If `auto_sync_enabled` is turned on in the UI, the app runs a small background worker in-process.

That worker:

- reads the saved creator/group sources
- waits for the configured interval
- pulls new Ballchasing replays
- downloads the replay files
- parses them locally
- adds them to the shelf

This is meant for a local single-user setup, not a hosted multi-user deployment.

## Cost profile

This path stays cheap:

- Ballchasing token: required
- Local machine: required
- GPT: not required
- YouTube API: not required
- RLTracker API: not required
- paid live data APIs: not required

## Notes

- The Ballchasing token is stored locally in `platform-config.json` because the app is designed for local use.
- The native viewer is the same local viewer stack used by the main ReplayOS project.
- This is best thought of as a review workstation, not a public hosted SaaS app.
