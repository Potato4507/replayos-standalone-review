from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW_DB = PROJECT_ROOT / "rl_frames_60hz.duckdb"
DEFAULT_SERVING_DB = PROJECT_ROOT / "data" / "replayos_serving.duckdb"
DEFAULT_REPLAY_DOWNLOAD_DIR = PROJECT_ROOT / "replays" / "ballchasing"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


def _split_origins(value: str | None) -> tuple[str, ...]:
    if not value:
        return ("http://127.0.0.1:5173", "http://localhost:5173")
    normalized = value.replace(";", ",")
    return tuple(origin.strip() for origin in normalized.split(",") if origin.strip())


def _split_values(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    normalized = value.replace(";", ",").replace("\n", ",")
    return tuple(piece.strip() for piece in normalized.split(",") if piece.strip())


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _load_env_file(path: Path = DEFAULT_ENV_FILE) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    raw_db: Path
    serving_db: Path
    replay_download_dir: Path
    cors_origins: tuple[str, ...]
    ballchasing_api_base: str
    ballchasing_api_token: str | None
    ballchasing_default_group: str | None
    ballchasing_default_groups: tuple[str, ...]
    ballchasing_default_creators: tuple[str, ...]
    ballchasing_default_creator_group_limit: int
    ballchasing_default_count: int
    youtube_api_key: str | None
    youtube_api_base: str
    youtube_default_count: int
    rocketleague_tv_url: str
    blast_rl_tournaments_url: str
    live_stream_cache_seconds: int
    live_schedule_cache_seconds: int
    replay_parse_retry_seconds: int
    frame_cache_entries: int
    sync_run_retention: int
    maintenance_enabled: bool
    maintenance_poll_seconds: int
    maintenance_index_interval_seconds: int
    maintenance_ballchasing_interval_seconds: int
    maintenance_carball_interval_seconds: int
    maintenance_eval_interval_seconds: int
    maintenance_youtube_interval_seconds: int
    maintenance_live_interval_seconds: int
    maintenance_ballchasing_count: int
    maintenance_parse_limit: int
    maintenance_eval_limit: int
    maintenance_youtube_limit: int
    sidecar_enabled: bool
    sidecar_sleep_seconds: int
    sidecar_parse_limit: int
    sidecar_eval_limit: int
    sidecar_ballchasing_count: int
    sidecar_youtube_limit: int
    sidecar_index_every: int
    sidecar_ballchasing_every: int
    sidecar_carball_every: int
    sidecar_eval_every: int
    sidecar_youtube_every: int
    sidecar_live_every: int
    api_title: str = "ReplayOS API"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_env_file()
    default_groups = _split_values(os.getenv("BALLCHASING_GROUP_IDS") or os.getenv("BALLCHASING_GROUP_ID"))
    return Settings(
        raw_db=Path(os.getenv("REPLAYOS_RAW_DB", str(DEFAULT_RAW_DB))),
        serving_db=Path(os.getenv("REPLAYOS_SERVING_DB", str(DEFAULT_SERVING_DB))),
        replay_download_dir=Path(os.getenv("REPLAYOS_REPLAY_DOWNLOAD_DIR", str(DEFAULT_REPLAY_DOWNLOAD_DIR))),
        cors_origins=_split_origins(os.getenv("REPLAYOS_CORS_ORIGINS")),
        ballchasing_api_base=os.getenv("BALLCHASING_API_BASE", "https://ballchasing.com/api").rstrip("/"),
        ballchasing_api_token=os.getenv("BALLCHASING_API_TOKEN") or None,
        ballchasing_default_group=default_groups[0] if default_groups else None,
        ballchasing_default_groups=default_groups,
        ballchasing_default_creators=_split_values(os.getenv("BALLCHASING_CREATOR_IDS")),
        ballchasing_default_creator_group_limit=int(os.getenv("BALLCHASING_CREATOR_GROUP_LIMIT", "12")),
        ballchasing_default_count=int(os.getenv("BALLCHASING_DEFAULT_COUNT", "25")),
        youtube_api_key=os.getenv("YOUTUBE_API_KEY") or None,
        youtube_api_base=os.getenv("YOUTUBE_API_BASE", "https://www.googleapis.com/youtube/v3").rstrip("/"),
        youtube_default_count=int(os.getenv("YOUTUBE_DEFAULT_COUNT", "12")),
        rocketleague_tv_url=os.getenv("ROCKETLEAGUE_TV_URL", "https://www.rocketleague.tv/").rstrip("/"),
        blast_rl_tournaments_url=os.getenv("BLAST_RL_TOURNAMENTS_URL", "https://blast.tv/rl/tournaments").rstrip("/"),
        live_stream_cache_seconds=int(os.getenv("REPLAYOS_LIVE_STREAM_CACHE_SECONDS", "60")),
        live_schedule_cache_seconds=int(os.getenv("REPLAYOS_LIVE_SCHEDULE_CACHE_SECONDS", "300")),
        replay_parse_retry_seconds=int(os.getenv("REPLAYOS_REPLAY_PARSE_RETRY_SECONDS", str(6 * 60 * 60))),
        frame_cache_entries=int(os.getenv("REPLAYOS_FRAME_CACHE_ENTRIES", "900")),
        sync_run_retention=int(os.getenv("REPLAYOS_SYNC_RUN_RETENTION", "200")),
        maintenance_enabled=_as_bool(os.getenv("REPLAYOS_MAINTENANCE_ENABLED"), True),
        maintenance_poll_seconds=int(os.getenv("REPLAYOS_MAINTENANCE_POLL_SECONDS", "15")),
        maintenance_index_interval_seconds=int(os.getenv("REPLAYOS_MAINTENANCE_INDEX_INTERVAL_SECONDS", str(30 * 60))),
        maintenance_ballchasing_interval_seconds=int(os.getenv("REPLAYOS_MAINTENANCE_BALLCHASING_INTERVAL_SECONDS", str(15 * 60))),
        maintenance_carball_interval_seconds=int(os.getenv("REPLAYOS_MAINTENANCE_CARBALL_INTERVAL_SECONDS", str(5 * 60))),
        maintenance_eval_interval_seconds=int(os.getenv("REPLAYOS_MAINTENANCE_EVAL_INTERVAL_SECONDS", str(5 * 60))),
        maintenance_youtube_interval_seconds=int(os.getenv("REPLAYOS_MAINTENANCE_YOUTUBE_INTERVAL_SECONDS", str(20 * 60))),
        maintenance_live_interval_seconds=int(os.getenv("REPLAYOS_MAINTENANCE_LIVE_INTERVAL_SECONDS", "60")),
        maintenance_ballchasing_count=int(os.getenv("REPLAYOS_MAINTENANCE_BALLCHASING_COUNT", "8")),
        maintenance_parse_limit=int(os.getenv("REPLAYOS_MAINTENANCE_PARSE_LIMIT", "8")),
        maintenance_eval_limit=int(os.getenv("REPLAYOS_MAINTENANCE_EVAL_LIMIT", "48")),
        maintenance_youtube_limit=int(os.getenv("REPLAYOS_MAINTENANCE_YOUTUBE_LIMIT", "6")),
        sidecar_enabled=_as_bool(os.getenv("REPLAYOS_SIDECAR_ENABLED"), True),
        sidecar_sleep_seconds=int(os.getenv("REPLAYOS_SIDECAR_SLEEP_SECONDS", "180")),
        sidecar_parse_limit=int(os.getenv("REPLAYOS_SIDECAR_PARSE_LIMIT", "1")),
        sidecar_eval_limit=int(os.getenv("REPLAYOS_SIDECAR_EVAL_LIMIT", "48")),
        sidecar_ballchasing_count=int(os.getenv("REPLAYOS_SIDECAR_BALLCHASING_COUNT", "6")),
        sidecar_youtube_limit=int(os.getenv("REPLAYOS_SIDECAR_YOUTUBE_LIMIT", "4")),
        sidecar_index_every=int(os.getenv("REPLAYOS_SIDECAR_INDEX_EVERY", "12")),
        sidecar_ballchasing_every=int(os.getenv("REPLAYOS_SIDECAR_BALLCHASING_EVERY", "12")),
        sidecar_carball_every=int(os.getenv("REPLAYOS_SIDECAR_CARBALL_EVERY", "1")),
        sidecar_eval_every=int(os.getenv("REPLAYOS_SIDECAR_EVAL_EVERY", "1")),
        sidecar_youtube_every=int(os.getenv("REPLAYOS_SIDECAR_YOUTUBE_EVERY", "6")),
        sidecar_live_every=int(os.getenv("REPLAYOS_SIDECAR_LIVE_EVERY", "1")),
    )
