# Standalone Replay Review

## What this is

`standalone_replay_review.py` is the smallest self-serve slice of ReplayOS that still feels useful:

- one Ballchasing replay id or URL
- one Ballchasing API token
- local replay download
- local 60 Hz Carball parse
- review cards
- win-edge timeline
- player impact
- native 3D replay viewer

It does **not** need GPT, YouTube, RLTracker, RLCS schedule APIs, or any paid add-ons.

## Who this is for

Use this when you want to hand someone a free local tool that reviews one replay at a time without asking them to run the full ReplayOS site and background pipeline.

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

Paste:

1. a Ballchasing replay URL or replay id
2. your Ballchasing API token
3. optionally check `Force re-download and re-parse`

Then click `Prepare review`.

## What it does on the first run

For a replay like `92aa7211-d35d-4b3c-b93f-8a9faf21ac24`, the standalone tool:

1. calls Ballchasing for replay metadata
2. downloads the `.replay` file locally
3. parses the replay into 60 Hz telemetry with the existing local Carball pipeline
4. stores a tiny standalone DuckDB workspace just for that replay
5. builds the review payload
6. serves the native viewer from the local static assets already in this repo

The per-replay workspace lands here:

```text
output/standalone-review/<replay-id>/
```

That folder contains:

- `standalone.duckdb`
- `replays/<replay-id>.replay`

## What it reuses from ReplayOS

This standalone app deliberately reuses the real internals instead of shipping a fake demo path:

- Ballchasing download flow from `replayos.ballchasing`
- 60 Hz replay parsing from `replayos.carball_ingest`
- review math from `replayos.site`
- native viewer payload generation from `replayos.native_viewer`
- native viewer frontend from `frontend/public/native-viewer`

That means the standalone output stays aligned with the main app instead of drifting into a separate half-maintained fork.

## Endpoints

Useful local routes:

- `/`  
  The standalone web UI.

- `/api/review`  
  POST JSON to prepare a replay and return the review payload.

- `/library/replays/<replay-id>/viewer`  
  The standalone review JSON for an already-prepared replay.

- `/library/replays/<replay-id>/native-viewer`  
  The native viewer payload route used by the embedded 3D viewer.

- `/library/replays/<replay-id>/file`  
  Download the local replay file.

## Example JSON request

```json
{
  "replay_input": "https://ballchasing.com/replay/92aa7211-d35d-4b3c-b93f-8a9faf21ac24",
  "ballchasing_api_token": "YOUR_TOKEN_HERE",
  "force_refresh": false
}
```

## Free stack

This standalone path is intentionally cheap:

- Ballchasing token: required
- Local CPU/GPU: required
- GPT: not required
- YouTube API: not required
- RLTracker API: not required
- Live RLCS schedule APIs: not required

## Design choices

### Why one replay at a time?

That keeps the setup simple:

- no site-wide background sync
- no giant shared database
- no paid enrichment dependencies
- easier to explain

### Why a local workspace per replay?

It makes the tool easy to reason about and easy to delete:

- each replay is isolated
- reruns are cheap
- users can keep just the replays they care about

## Limitations

- It is a **local single-user tool**, not a multi-tenant hosted service.
- The standalone app temporarily overrides a few ReplayOS env-backed settings during request handling so it can point the reused internals at the standalone workspace.
- The 3D viewer is only as good as the local native viewer assets and current camera logic.
- This version focuses on replay review, not the larger tournament/live-site feature set.

## Shipping this to GitHub

The easiest publish shape is:

1. keep `standalone_replay_review.py`
2. keep this doc
3. keep the existing native viewer assets in the repo
4. tell users to run only:

```powershell
pip install -e .
python standalone_replay_review.py
```

That gives people a very short path from clone to usable replay review.
