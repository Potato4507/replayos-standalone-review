from __future__ import annotations

from pydantic import BaseModel, Field


class AnalystQuery(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    replay_id: str | None = None


class RefreshRequest(BaseModel):
    train_models: bool = True
    sample_limit: int | None = Field(default=None, ge=1)


class BallchasingSyncRequest(BaseModel):
    group_id: str | None = None
    creator_id: str | None = None
    playlist: str | None = None
    player_name: list[str] = Field(default_factory=list)
    player_id: list[str] = Field(default_factory=list)
    count: int = Field(default=25, ge=1, le=200)
    download_files: bool = True
    fetch_details: bool = True
    force_download: bool = False
    parse_downloads: bool = True


class YouTubeSyncRequest(BaseModel):
    replay_id: str | None = None
    limit: int = Field(default=12, ge=1, le=100)


class CarballBackfillRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=500)
    force: bool = False
    refresh_index: bool = True


class EvalBackfillRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=1000)
    force: bool = False


class MaintenanceRunRequest(BaseModel):
    refresh_index: bool = True
    refresh_ballchasing: bool = True
    backfill_names: bool = True
    refresh_youtube: bool = True
    backfill_eval: bool = True
    refresh_live: bool = True
    parse_limit: int = Field(default=24, ge=1, le=500)
    eval_limit: int = Field(default=120, ge=1, le=2000)
    ballchasing_count: int = Field(default=8, ge=1, le=200)
    youtube_limit: int = Field(default=6, ge=1, le=100)
    force_eval: bool = False
