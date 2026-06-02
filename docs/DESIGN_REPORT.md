# ReplayOS Design Report

## Architecture

ReplayOS is organized into five layers.

1. Data: the large frame DuckDB stays read-only. A reproducible warehouse builder creates a compact serving DuckDB with normalized replays, matches, teams, players, events, features, predictions, model versions, and lineage.
2. Semantics: existing derived frame events are promoted into API-ready timelines. Additional helpers derive touch chains, possession phases, loose-ball resets, and turning points.
3. Analytics: the model pipeline trains a supervised blue-side win predictor and an unsupervised team-style clustering model. Each run writes metrics, calibration, artifacts, and reason codes.
4. Product: FastAPI exposes stable endpoints. The React site gives a live RLCS/pro watch desk, match hub, series board, 3D replay viewer, rankings, analyst workflows, replay downloads, and synced YouTube video embeds.
5. Analyst workflow: `/analyst/query` routes natural-language questions to reproducible query templates, and `/reports/matchup` exports a Markdown scouting report.

## Service Boundaries

`replayos.warehouse` owns raw-to-serving transforms. `replayos.ballchasing` owns external replay sync and file downloading. `replayos.analytics` owns model training, experiment tracking, prediction generation, and matchup scoring. `replayos.semantics` owns event timeline derivation. `replayos.site` owns public-facing match, series, replay-viewer, and ELO aggregation queries. `replayos.api` is intentionally thin and delegates to those modules.

`replayos.frames` exposes sampled 3D telemetry from the raw frame bucket store. `replayos.carball_ingest` closes the gap for downloaded `.replay` files by parsing them through Carball, resampling them to a cached 60 Hz payload, and deriving local touch/goal/turnover/pressure events. `replayos.youtube_sync` scores candidate YouTube videos against replay metadata using team-name overlap, series-name overlap, Rocket League keyword context, publish-date alignment, and light popularity weighting. `replayos.live_sync` caches BLAST Rocket League tournament pages plus RocketLeague.tv live listings to drive the live site surfaces.

## Semantic Derivation

The corpus already contains event types such as `touch`, `possession_start`, `possession_end`, `loose_ball_start`, `goal`, `demo`, and `boost_starvation_window`. ReplayOS preserves those events and derives:

- Touch chains split by player, team, and time gap.
- Possession phases closed by explicit end, goal, loose ball, or a new possession start.
- Turning points from goals, demos, boost starvation windows, and loose-ball resets.
- Turnover events inferred from color changes between possession starts.
- Kickoff, pressure, and overcommit proxy events projected from aggregate replay features.
- Team-match feature rows for possession, attack-zone rate, boost, aerial rate, pressure, starvation, overcommit, and goal pressure.
- Player-match impact rows from touches, goals, demos, possession starts, and starvation windows.

## Modeling Choices

The win prediction model is a small logistic regression implemented in Numpy. It uses deterministic replay-id holdout splitting and excludes direct outcome columns such as goals and `blue_win_label` from predictors. The baseline is a majority-probability model from the training split.

The style model is Numpy k-means over team-match features. It is compared against a single-cluster baseline using inertia reduction. Cluster names are assigned from the strongest standardized center characteristics, such as pressure-forward, boost-control, aerial-tempo, and risk-heavy.

## Product Surfaces

The site starts in the working match hub rather than a marketing page. It includes:

- Series board and match feed inspired by BLAST-style live hubs and Liquipedia-style information density.
- Replay viewer with turning points, downloadable files, box scores, a win-edge swing bar, a sampled 3D telemetry scene, and synced YouTube embeds.
- Live radar for active RLCS/upcoming tournament focus, official watch-channel lists, and pro/co-stream discovery.
- Team ELO ladder computed from downloaded match chronology.
- Analyst query form backed by deterministic data queries.
- Meta exploration for event distributions and player impact.

## Limitations

The inspected raw schema does not include stable organization/team names, series metadata, true roster history, or external betting/seed data. Upset detection and roster fit estimation are therefore framed as extensible modules rather than overclaimed outputs. The current team entities are honest synthetic replay-side teams.

## Deployment Plan

For local development, run the warehouse/model pipeline, start FastAPI with Uvicorn, then start the Vite dashboard. For production, schedule `scripts/run_pipeline.py` as a batch job, keep live sync inside the API process or a single writer worker, serve the API behind a reverse proxy, and publish the built frontend from `frontend/dist`.
