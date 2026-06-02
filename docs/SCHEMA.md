# ReplayOS Warehouse Schema

The raw `rl_frames_60hz.duckdb` database remains the immutable source. `scripts/run_pipeline.py` creates `data/replayos_serving.duckdb`, which separates normalized warehouse tables from model outputs and API-serving aggregates.

## Core Tables

| Table | Primary key | Source | Join strategy | Refresh cadence |
| --- | --- | --- | --- | --- |
| `replays` | `replay_id` | `raw.ingested`, `raw.replay_semantic_features` | Direct replay id | Batch refresh |
| `matches` | `match_id` | `replays`, `features_replay` | `match_id = replay_id` | Batch refresh |
| `teams` | `team_id` | `matches` | `team_id = replay_id:color` | Batch refresh |
| `players` | `player_id` | `events` | Source player id | Batch refresh |
| `events` | `event_id` | `raw.derived_events` | Replay timestamp window ordering | Batch refresh |
| `features_replay` | `replay_id` | `raw.replay_semantic_features`, `events` | Direct replay id | Batch refresh |
| `features_team_match` | `team_id` | `features_replay` | Wide blue/orange features normalized to team rows | Batch refresh |
| `features_player_match` | `player_match_id` | `events` | `replay_id:player_id` | Batch refresh |
| `predictions` | `prediction_id` | Model pipeline | `model_version_id + target_id` | Model run |
| `model_versions` | `model_version_id` | Model pipeline | Generated run id | Model run |
| `experiment_runs` | `experiment_id` | Model pipeline | Generated run id | Model run |
| `lineage` | `table_name` | Static metadata | Table-level notes | Batch refresh |
| `remote_sync_runs` | `run_id` | Ballchasing sync | Generated sync id | On demand / scheduled |
| `remote_groups` | `group_id` | Ballchasing group API | Ballchasing group id | On demand / scheduled |
| `remote_replays` | `replay_id` | Ballchasing replay API | Ballchasing replay id | On demand / scheduled |
| `remote_players` | `(replay_id, side, platform, platform_player_id)` | Ballchasing replay detail API | Replay id + player id | On demand / scheduled |
| `remote_replay_groups` | `(replay_id, group_id)` | Ballchasing replay detail API | Replay id + group id | On demand / scheduled |
| `replay_parsed_status` | `replay_id` | Carball replay parse pipeline | Replay id | On demand / scheduled |
| `replay_parsed_frames` | `replay_id` | Carball replay parse pipeline | Replay id | On demand / scheduled |
| `replay_parsed_events` | `(replay_id, event_id)` | Carball replay parse pipeline | Replay id + ordered event id | On demand / scheduled |
| `youtube_sync_runs` | `run_id` | YouTube search/video API | Generated sync id | On demand / scheduled |
| `replay_videos` | `(replay_id, video_id)` | YouTube search/video API | Replay id + YouTube video id | On demand / scheduled |
| `live_sync_runs` | `run_id` | BLAST page cache + RocketLeague.tv cache | Generated sync id | On demand / scheduled |
| `live_tournaments` | `tournament_slug` | BLAST tournament index + event pages | Tournament slug | On demand / scheduled |
| `live_watch_channels` | `(tournament_slug, language, channel_name)` | BLAST series pages | Tournament slug + channel | On demand / scheduled |
| `live_matches` | `match_id` | BLAST series pages | Tournament slug + local order | On demand / scheduled |
| `live_streams` | `channel_name` | RocketLeague.tv ItemList metadata | Channel name | On demand / scheduled |

## Raw-to-Serving Lineage

`raw.ingested` becomes the replay registry. `raw.derived_events` is copied into `events` with stable event ids and color-side team ids. ReplayOS then appends inferred turnover events and aggregate kickoff, pressure, and overcommit proxy events with lineage notes. `raw.replay_semantic_features` becomes `features_replay`, then is normalized into `features_team_match`. Player-level rows are aggregated from event counts and weighted into an interpretable `impact_score`.

Ballchasing sync writes replay metadata, replay-group mappings, group summaries, downloaded file paths, and player box scores into the `remote_*` tables. When replay ids overlap with the local frame warehouse, the site joins remote team names and series metadata onto local semantic analytics.

Carball replay parsing writes file-stable parse status into `replay_parsed_status`, compressed 60 Hz replay telemetry into `replay_parsed_frames`, and lightweight semantic events into `replay_parsed_events`. These tables let downloaded replays behave like first-class viewer items even when they were never present in the original raw DuckDB corpus.

YouTube sync writes scored replay-to-video matches into `replay_videos`. The current heuristic is intentionally transparent: title overlap with blue/orange team names and series name, Rocket League keyword presence, publish-date alignment, and a small popularity bonus.

Live sync writes upcoming/live RLCS tournament summaries, watch channels, parsed match slates, and filtered RLCS/pro stream listings into the `live_*` tables. The cache windows are intentionally short for streams and longer for schedule pages so the site can poll frequently without a proportional increase in remote requests.

## Outcome Labels

`blue_win_label` is inferred from goal events in `events`. Ties or missing goal separation are left as `NULL` and excluded from supervised win prediction. This avoids creating artificial labels where the source does not provide a winner.

## Team Identity Assumption

The inspected raw tables do not include stable esports organization names. ReplayOS therefore treats blue and orange as synthetic teams per replay. When real roster/team metadata is added, `teams.team_id` can become an organization id, while `features_team_match` remains the correct long-form match feature table.
