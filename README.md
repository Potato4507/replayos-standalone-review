# ReplayOS

ReplayOS is a production-style Rocket League replay intelligence platform built around the existing `rl_frames_60hz.duckdb` corpus. It keeps the large frame database as read-only raw storage, builds a small serving warehouse, trains versioned models, exposes FastAPI endpoints, and provides a React dashboard for replay exploration, matchup analysis, model explainability, and analyst workflows.

## What is included

- Reproducible warehouse refresh from raw DuckDB tables into normalized serving tables.
- FastAPI service for replays, teams, players, events, features, predictions, model versions, matchup comparisons, analyst queries, and report export.
- Semantic timeline helpers for touch chains, possessions, loose balls, and turning points.
- Numpy-based win prediction and team style clustering with baseline comparisons, metrics, calibration, artifacts, and reason codes.
- Ballchasing sync and replay downloading into a local library with series, player box scores, and replay-file access.
- Maintained Carball-based replay parsing that converts downloaded `.replay` files into cached 60 Hz telemetry and local semantic events.
- YouTube replay-video matching with scored sync heuristics over team names, series names, and dates, using a public no-key search path by default.
- RLCS and pro-stream live sync that caches BLAST schedule pages and RocketLeague.tv stream listings for minute-level site refreshes.
- Public-facing React site backed by live API queries, with a live radar, match hub, 3D replay viewer, video embeds, ELO ladder, and analyst desk.
- Design, schema, and model evaluation documentation under `docs/`.

## Setup

```powershell
cd D:\RocketLeagueFrames
python -m pip install -e .[dev]
```

Create `.env` from `.env.example` if the database paths differ.

If you want the frontend to call a different backend, set:

```powershell
cd D:\RocketLeagueFrames\frontend
$env:VITE_API_BASE="http://127.0.0.1:8000"
npm run dev
```

If you leave `VITE_API_BASE` unset, the site defaults to `http://127.0.0.1:8000`.

For the core local site, no extra key is required.

If you want the review-only local platform instead of the full site, run:

```powershell
python standalone_replay_review.py
```

That starts the local ReplayOS review platform at `http://127.0.0.1:8010`. It has:

- saved Ballchasing token support
- creator/group source sync
- optional auto-sync
- a replay shelf
- the review viewer stack

Full instructions are in [docs/standalone-replay-review.md](/D:/RocketLeagueFrames/docs/standalone-replay-review.md).

To enable replay downloading from Ballchasing, set:

```powershell
BALLCHASING_API_TOKEN=...
BALLCHASING_GROUP_ID=...
BALLCHASING_GROUP_IDS=...
BALLCHASING_CREATOR_IDS=...
```

Optional extras:

```powershell
YOUTUBE_API_KEY=...
```

- `BALLCHASING_API_TOKEN` is the only required key for replay metadata and `.replay` downloads.
- `BALLCHASING_GROUP_ID` still works for one pinned source, but ReplayOS can now also fan out across comma-separated `BALLCHASING_GROUP_IDS` and creator feeds in `BALLCHASING_CREATOR_IDS`.
- `YOUTUBE_API_KEY` is optional. ReplayOS can search public YouTube data through `yt-dlp` with no key, and the API key only switches it to the official Data API path.
- The analyst desk runs locally and does not need an API key.
- The live RLCS and leaderboard views use public BLAST and RocketLeague.tv pages, not paid APIs.

In practice, the site now runs with zero paid API requirements. The recurring costs are hosting, storage, and bandwidth, not third-party data APIs.

The maintained package that exposes `import carball` on this runtime is `sprocket_carball`, which is already declared in `pyproject.toml`.

ReplayOS also pins the maintained `sprocket-boxcars-py` parser backend from SprocketBot's GitHub repo. That newer Boxcars build is what unlocks modern replay header parsing on Python 3.13, including recent Ballchasing tournament replays that older wheels rejected with `StructProperty` header errors. On Windows this build path requires a working Rust toolchain.

## Rebuild the platform

```powershell
python scripts\run_pipeline.py
```

For a quick smoke build against a subset of replays:

```powershell
python scripts\run_pipeline.py --sample-limit 500
```

The serving database is written to `data\replayos_serving.duckdb`.

## Sync Ballchasing replays

```powershell
python scripts\sync_ballchasing.py --group-id your-group-id --count 12
```

Creator feed sync:

```powershell
python scripts\sync_ballchasing.py --creator-id 76561199225615730 --count 12
```

If you leave `--group-id` and `--creator-id` unset, the script now uses the configured default groups plus default creator feeds from `.env`. Tournament root groups are expanded recursively through child groups before replay sync begins, so season folders like RLCS and event folders like EWC can be used directly.

Metadata-only sync:

```powershell
python scripts\sync_ballchasing.py --group-id your-group-id --count 12 --metadata-only
```

Downloaded `.replay` files are stored under `replays\ballchasing`.

Each sync also attempts to parse the downloaded replay files through Carball and cache a 60 Hz replay payload. Repeated syncs skip files that have already been parsed unless the file changed, the parser version advanced, or you force a re-download.

## Expand real-name coverage across the local replay library

```powershell
python scripts\backfill_carball.py --limit 24
```

ReplayOS now keeps a cached index of local `.replay` files under `replays\`, including files that are present on disk but not yet represented in the warehouse tables. The backfill runner uses that index to:

- prioritize the highest-value unparsed replays first
- expand accurate team and player naming coverage for records, rankings, and the 3D viewer
- include local-only orphan replay files in the named corpus

If you only want to refresh the replay-file inventory:

```powershell
python scripts\backfill_carball.py --scan-only
```

## Sync YouTube videos

```powershell
python scripts\sync_youtube.py --limit 8
```

Sync only one replay:

```powershell
python scripts\sync_youtube.py --replay-id your-replay-id --limit 1
```

ReplayOS now tries two layers:

- direct replay-video matches for standalone game uploads
- long tournament VOD matching, followed by chapter parsing and replay-sized clip assignment

By default, this sync uses `yt-dlp` against public YouTube pages. If `YOUTUBE_API_KEY` is present, ReplayOS will use the official YouTube Data API instead.

When the VOD description includes timestamp chapters such as `Game 1`, `Game 2`, and team labels, the viewer stores replay-specific segment start/end times and opens the embed at the correct game slice instead of the top of the full broadcast.

## Sync live RLCS and pro-stream data

```powershell
python scripts\sync_live.py --force
```

The live sync uses cached copies of:

- BLAST Rocket League tournament pages for current RLCS schedule slices and watch channels.
- RocketLeague.tv for live RLCS/co-stream/pro-stream listings.

The API can refresh this data in-process, and the site polls the cached live endpoints every minute.

## Automatic upkeep worker

When the API starts, ReplayOS now launches a lightweight background worker in-process. That worker keeps the site fresh by periodically running:

- live RLCS and stream sync
- Ballchasing replay sync against the configured default groups and creator feeds
- local replay-file indexing
- Carball parse backfill
- YouTube and VOD matching
- replay eval cache backfill

The defaults are tuned to stay cheap on one machine:

```powershell
REPLAYOS_MAINTENANCE_ENABLED=true
REPLAYOS_MAINTENANCE_POLL_SECONDS=15
REPLAYOS_MAINTENANCE_LIVE_INTERVAL_SECONDS=60
REPLAYOS_MAINTENANCE_BALLCHASING_INTERVAL_SECONDS=900
REPLAYOS_MAINTENANCE_INDEX_INTERVAL_SECONDS=1800
REPLAYOS_MAINTENANCE_CARBALL_INTERVAL_SECONDS=300
REPLAYOS_MAINTENANCE_EVAL_INTERVAL_SECONDS=300
REPLAYOS_MAINTENANCE_YOUTUBE_INTERVAL_SECONDS=1200
REPLAYOS_MAINTENANCE_BALLCHASING_COUNT=8
REPLAYOS_MAINTENANCE_PARSE_LIMIT=8
REPLAYOS_MAINTENANCE_EVAL_LIMIT=48
REPLAYOS_MAINTENANCE_YOUTUBE_LIMIT=6
```

You can also run the same pass manually from the CLI:

```powershell
python scripts\run_maintenance.py
```

Or hit the API:

```powershell
GET /sources/maintenance/status
POST /sources/maintenance/run
```

## Run the API

```powershell
uvicorn replayos.api:app --host 127.0.0.1 --port 8000 --reload
```

Useful endpoints:

- `GET /health`
- `GET /summary`
- `GET /site/home`
- `GET /site/live`
- `GET /site/teams/elo`
- `GET /series`
- `GET /library/replays`
- `GET /library/replays/{id}/viewer`
- `GET /library/replays/{id}/frames`
- `GET /library/replays/{id}/videos`
- `GET /library/replays/{id}/file`
- `GET /sources/ballchasing/status`
- `POST /sources/ballchasing/sync`
- `GET /sources/carball/status`
- `POST /sources/carball/index`
- `POST /sources/carball/backfill`
- `GET /sources/maintenance/status`
- `POST /sources/maintenance/run`
- `GET /sources/youtube/status`
- `POST /sources/youtube/sync`
- `GET /sources/live/status`
- `POST /sources/live/sync`
- `GET /replays`
- `GET /teams`
- `GET /players`
- `GET /warehouse/schema`
- `GET /matchups/compare?team_a_id=...&team_b_id=...`
- `POST /analyst/query`
- `GET /reports/matchup?team_a_id=...&team_b_id=...`

## Run the dashboard

```powershell
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`.

The current front page is a stats-and-games hub: RLCS live radar, series board, match feed, 3D replay viewer, synced YouTube embeds, download links, ELO ladder, player table, and analyst queries.

## Performance Notes

- Raw 733 GB frame storage remains read-only; request-serving data stays in the smaller serving DuckDB.
- Carball replay parses are incremental and keyed by file size, mtime, and parser version.
- Failed replay parses now use a retry cooldown, so one broken `.replay` file does not get reparsed on every viewer hit or sync pass.
- Parsed replay payloads are compressed before storage, and sampled viewer payloads are cached separately.
- The viewer-level frame cache prunes old entries to avoid unbounded growth, and the entry cap is configurable.
- Live sync calls are cache-windowed so the page can poll every minute without re-scraping remote sources on every request.
- Ballchasing and live-sync run history are pruned automatically, and the team ELO ladder is cached so it is not recomputed from every replay on every page load.

## Tests

```powershell
python -m unittest discover -s tests
```

## Assumptions

The raw corpus does not expose real organization/team metadata in the inspected tables. ReplayOS therefore models each color side as a synthetic side-team using `team_id = replay_id:color`. This keeps joins reproducible and avoids pretending that blue/orange sides are stable esports organizations. The design report documents the implications and upgrade path.
