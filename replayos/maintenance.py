from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from .ballchasing import configured_ballchasing_sources, sync_ballchasing_source_set
from .carball_ingest import backfill_replay_names, refresh_local_replay_index
from .config import get_settings
from .db import jsonable, refresh_read_replica, serving_connection
from .live_sync import LiveSyncError, sync_live_data
from .site import ballchasing_status, refresh_replay_review_cache, replay_review_status, team_elo_index
from .youtube_sync import YouTubeClient, sync_youtube_videos, youtube_status


class MaintenanceRunLockedError(RuntimeError):
    def __init__(self, details: dict[str, Any]):
        super().__init__("Maintenance writer lock is already held")
        self.details = details


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_interval(value: int, fallback: int) -> int:
    return max(5, int(value or fallback))


def _source_defaults_available() -> bool:
    sources = configured_ballchasing_sources()
    return bool(sources["groups"] or sources["creators"])


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _writer_lock_path(serving_db: Path) -> Path:
    return Path(serving_db).with_suffix(f"{Path(serving_db).suffix}.maintenance.lock")


def _read_writer_lock(lock_path: Path) -> dict[str, Any] | None:
    if not lock_path.exists():
        return None
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"path": str(lock_path)}
    payload["path"] = str(lock_path)
    return payload


def _write_writer_lock(lock_path: Path, payload: dict[str, Any]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    descriptor = os.open(str(lock_path), flags)
    try:
        os.write(descriptor, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(descriptor)


class _MaintenanceWriterLock:
    def __init__(self, serving_db: Path):
        self._lock_path = _writer_lock_path(serving_db)
        self._payload: dict[str, Any] | None = None

    def acquire(self) -> dict[str, Any]:
        payload = {
            "pid": os.getpid(),
            "started_at": _utcnow().isoformat(),
        }
        for _ in range(2):
            try:
                _write_writer_lock(self._lock_path, payload)
                self._payload = payload
                return {"path": str(self._lock_path), **payload}
            except FileExistsError as exc:
                current = _read_writer_lock(self._lock_path) or {"path": str(self._lock_path)}
                current_pid = current.get("pid")
                if current_pid and _pid_alive(int(current_pid)):
                    raise MaintenanceRunLockedError(current) from exc
                try:
                    self._lock_path.unlink()
                except OSError:
                    current = _read_writer_lock(self._lock_path) or current
                    raise MaintenanceRunLockedError(current) from exc
        current = _read_writer_lock(self._lock_path) or {"path": str(self._lock_path)}
        raise MaintenanceRunLockedError(current)

    def release(self) -> None:
        if not self._payload:
            return
        current = _read_writer_lock(self._lock_path)
        if current and current.get("pid") != self._payload.get("pid"):
            self._payload = None
            return
        try:
            self._lock_path.unlink()
        except OSError:
            pass
        self._payload = None


def run_maintenance_pass(
    *,
    trigger: str,
    refresh_index: bool,
    refresh_ballchasing: bool,
    backfill_names: bool,
    refresh_youtube: bool,
    backfill_eval: bool,
    refresh_live: bool,
    parse_limit: int | None = None,
    eval_limit: int | None = None,
    ballchasing_count: int | None = None,
    youtube_limit: int | None = None,
    force_eval: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    started_at = _utcnow()
    parse_limit = max(1, int(parse_limit or settings.maintenance_parse_limit))
    eval_limit = max(1, int(eval_limit or settings.maintenance_eval_limit))
    ballchasing_count = max(1, int(ballchasing_count or settings.maintenance_ballchasing_count))
    youtube_limit = max(1, int(youtube_limit or settings.maintenance_youtube_limit))
    results: dict[str, Any] = {
        "trigger": trigger,
        "started_at": started_at.isoformat(),
        "steps": {},
    }
    writer_lock = _MaintenanceWriterLock(settings.serving_db)
    try:
        results["lock"] = writer_lock.acquire()
    except MaintenanceRunLockedError as exc:
        completed_at = _utcnow()
        results["status"] = "busy"
        results["reason"] = "writer_locked"
        results["lock"] = exc.details
        results["completed_at"] = completed_at.isoformat()
        results["duration_seconds"] = round((completed_at - started_at).total_seconds(), 3)
        return results

    try:
        refreshed_index = False
        if refresh_live:
            try:
                results["steps"]["live"] = sync_live_data(settings.serving_db, force=False)
            except LiveSyncError as exc:
                results["steps"]["live"] = {"status": "error", "error": str(exc)}

        if refresh_ballchasing:
            if not settings.ballchasing_api_token:
                results["steps"]["ballchasing"] = {"status": "skipped", "reason": "BALLCHASING_API_TOKEN is not configured"}
            elif not _source_defaults_available():
                results["steps"]["ballchasing"] = {"status": "skipped", "reason": "No default Ballchasing groups or creator feeds are configured"}
            else:
                try:
                    results["steps"]["ballchasing"] = sync_ballchasing_source_set(
                        settings.serving_db,
                        group_ids=settings.ballchasing_default_groups,
                        creator_ids=settings.ballchasing_default_creators,
                        creator_group_limit=settings.ballchasing_default_creator_group_limit,
                        count=ballchasing_count,
                        download_files=True,
                        fetch_details=True,
                        force_download=False,
                        parse_downloads=True,
                    )
                except Exception as exc:
                    results["steps"]["ballchasing"] = {"status": "error", "error": str(exc)}

        if refresh_index:
            results["steps"]["index"] = refresh_local_replay_index(serving_db=settings.serving_db)
            refreshed_index = True

        if backfill_names:
            try:
                results["steps"]["carball"] = backfill_replay_names(
                    serving_db=settings.serving_db,
                    limit=parse_limit,
                    force=False,
                    refresh_index=not refreshed_index,
                )
            except Exception as exc:
                results["steps"]["carball"] = {"status": "error", "error": str(exc)}

        if refresh_youtube:
            provider = YouTubeClient.provider_status(api_key=settings.youtube_api_key)
            if not provider.get("sync_enabled"):
                results["steps"]["youtube"] = {"status": "skipped", "reason": "No YouTube sync provider is available", **provider}
            else:
                try:
                    results["steps"]["youtube"] = sync_youtube_videos(settings.serving_db, limit=youtube_limit)
                except Exception as exc:
                    results["steps"]["youtube"] = {"status": "error", "error": str(exc), **provider}

        if backfill_eval:
            try:
                with duckdb.connect(str(settings.serving_db)) as con:
                    results["steps"]["eval"] = refresh_replay_review_cache(con, limit=eval_limit, force=force_eval)
            except Exception as exc:
                results["steps"]["eval"] = {"status": "error", "error": str(exc)}

        try:
            with duckdb.connect(str(settings.serving_db)) as con:
                ladder = team_elo_index(con, limit=32)
                results["steps"]["rankings"] = {"status": "ok", "cached": len(ladder)}
        except Exception as exc:
            results["steps"]["rankings"] = {"status": "error", "error": str(exc)}

        replica = refresh_read_replica(settings.serving_db)
        results["replica"] = {
            "path": str(replica) if replica else None,
            "refreshed": bool(replica),
        }
    finally:
        writer_lock.release()

    with serving_connection(read_only=True) as con:
        results["status"] = {
            "ballchasing": ballchasing_status(con),
            "youtube": youtube_status(con),
            "eval": replay_review_status(con),
        }

    completed_at = _utcnow()
    results["completed_at"] = completed_at.isoformat()
    results["duration_seconds"] = round((completed_at - started_at).total_seconds(), 3)
    return results


@dataclass
class _WorkerStepState:
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_result: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_started_at": _iso(self.last_started_at),
            "last_completed_at": _iso(self.last_completed_at),
            "last_result": copy.deepcopy(self.last_result),
        }


@dataclass
class _WorkerState:
    enabled: bool
    running: bool = False
    current_trigger: str | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_result: dict[str, Any] | None = None
    last_error: str | None = None
    steps: dict[str, _WorkerStepState] = field(
        default_factory=lambda: {
            "live": _WorkerStepState(),
            "ballchasing": _WorkerStepState(),
            "index": _WorkerStepState(),
            "carball": _WorkerStepState(),
            "youtube": _WorkerStepState(),
            "eval": _WorkerStepState(),
        }
    )

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "current_trigger": self.current_trigger,
            "last_started_at": _iso(self.last_started_at),
            "last_completed_at": _iso(self.last_completed_at),
            "last_result": copy.deepcopy(self.last_result),
            "last_error": self.last_error,
            "steps": {name: step.snapshot() for name, step in self.steps.items()},
        }


class MaintenanceWorker:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = _WorkerState(enabled=self._settings.maintenance_enabled and not self._settings.sidecar_enabled)

    def start(self) -> None:
        if not self._settings.maintenance_enabled or self._settings.sidecar_enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._wake_event.set()
        self._thread = threading.Thread(target=self._loop, name="replayos-maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=10)

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            state = self._state.snapshot()
        state["thread_alive"] = bool(self._thread and self._thread.is_alive())
        state["poll_seconds"] = self._settings.maintenance_poll_seconds
        state["intervals"] = {
            "live": self._settings.maintenance_live_interval_seconds,
            "ballchasing": self._settings.maintenance_ballchasing_interval_seconds,
            "index": self._settings.maintenance_index_interval_seconds,
            "carball": self._settings.maintenance_carball_interval_seconds,
            "youtube": self._settings.maintenance_youtube_interval_seconds,
            "eval": self._settings.maintenance_eval_interval_seconds,
        }
        state["batch_limits"] = {
            "ballchasing_count": self._settings.maintenance_ballchasing_count,
            "parse_limit": self._settings.maintenance_parse_limit,
            "youtube_limit": self._settings.maintenance_youtube_limit,
            "eval_limit": self._settings.maintenance_eval_limit,
        }
        return state

    def run_now(
        self,
        *,
        trigger: str = "manual",
        refresh_index: bool = True,
        refresh_ballchasing: bool = True,
        backfill_names: bool = True,
        refresh_youtube: bool = True,
        backfill_eval: bool = True,
        refresh_live: bool = True,
        parse_limit: int | None = None,
        eval_limit: int | None = None,
        ballchasing_count: int | None = None,
        youtube_limit: int | None = None,
        force_eval: bool = False,
    ) -> dict[str, Any]:
        acquired = self._run_lock.acquire(blocking=False)
        if not acquired:
            return {"status": "busy", "worker": self.snapshot()}
        try:
            return self._execute_pass(
                trigger=trigger,
                refresh_index=refresh_index,
                refresh_ballchasing=refresh_ballchasing,
                backfill_names=backfill_names,
                refresh_youtube=refresh_youtube,
                backfill_eval=backfill_eval,
                refresh_live=refresh_live,
                parse_limit=parse_limit,
                eval_limit=eval_limit,
                ballchasing_count=ballchasing_count,
                youtube_limit=youtube_limit,
                force_eval=force_eval,
            )
        finally:
            self._run_lock.release()

    def _loop(self) -> None:
        poll_seconds = _coerce_interval(self._settings.maintenance_poll_seconds, 15)
        while not self._stop_event.is_set():
            flags = self._due_flags()
            if any(flags.values()) or self._wake_event.is_set():
                self._wake_event.clear()
                acquired = self._run_lock.acquire(blocking=False)
                if acquired:
                    try:
                        expanded = dict(flags)
                        if expanded.get("ballchasing"):
                            expanded["index"] = True
                            expanded["carball"] = True
                            expanded["youtube"] = True
                            expanded["eval"] = True
                        try:
                            self._execute_pass(
                                trigger="auto",
                                refresh_index=expanded["index"],
                                refresh_ballchasing=expanded["ballchasing"],
                                backfill_names=expanded["carball"],
                                refresh_youtube=expanded["youtube"],
                                backfill_eval=expanded["eval"],
                                refresh_live=expanded["live"],
                            )
                        except Exception:
                            # Background upkeep should record the failure and keep ticking.
                            pass
                    finally:
                        self._run_lock.release()
            self._stop_event.wait(poll_seconds)

    def _due_flags(self) -> dict[str, bool]:
        now = _utcnow()
        with self._state_lock:
            steps = self._state.steps
            intervals = {
                "live": self._settings.maintenance_live_interval_seconds,
                "ballchasing": self._settings.maintenance_ballchasing_interval_seconds,
                "index": self._settings.maintenance_index_interval_seconds,
                "carball": self._settings.maintenance_carball_interval_seconds,
                "youtube": self._settings.maintenance_youtube_interval_seconds,
                "eval": self._settings.maintenance_eval_interval_seconds,
            }
            flags: dict[str, bool] = {}
            for name, interval in intervals.items():
                last = steps[name].last_completed_at
                flags[name] = last is None or (now - last).total_seconds() >= max(5, int(interval))
            return flags

    def _execute_pass(
        self,
        *,
        trigger: str,
        refresh_index: bool,
        refresh_ballchasing: bool,
        backfill_names: bool,
        refresh_youtube: bool,
        backfill_eval: bool,
        refresh_live: bool,
        parse_limit: int | None = None,
        eval_limit: int | None = None,
        ballchasing_count: int | None = None,
        youtube_limit: int | None = None,
        force_eval: bool = False,
    ) -> dict[str, Any]:
        started_at = _utcnow()
        with self._state_lock:
            self._state.running = True
            self._state.current_trigger = trigger
            self._state.last_started_at = started_at
            self._state.last_error = None
            for key, enabled in {
                "live": refresh_live,
                "ballchasing": refresh_ballchasing,
                "index": refresh_index,
                "carball": backfill_names,
                "youtube": refresh_youtube,
                "eval": backfill_eval,
            }.items():
                if enabled:
                    self._state.steps[key].last_started_at = started_at
        try:
            result = run_maintenance_pass(
                trigger=trigger,
                refresh_index=refresh_index,
                refresh_ballchasing=refresh_ballchasing,
                backfill_names=backfill_names,
                refresh_youtube=refresh_youtube,
                backfill_eval=backfill_eval,
                refresh_live=refresh_live,
                parse_limit=parse_limit,
                eval_limit=eval_limit,
                ballchasing_count=ballchasing_count,
                youtube_limit=youtube_limit,
                force_eval=force_eval,
            )
        except Exception as exc:
            completed_at = _utcnow()
            with self._state_lock:
                self._state.running = False
                self._state.current_trigger = None
                self._state.last_completed_at = completed_at
                self._state.last_error = str(exc)
            raise

        completed_at = _utcnow()
        with self._state_lock:
            self._state.running = False
            self._state.current_trigger = None
            self._state.last_completed_at = completed_at
            self._state.last_result = copy.deepcopy(result)
            self._state.last_error = None
            for key, step_result in result.get("steps", {}).items():
                if key in self._state.steps:
                    self._state.steps[key].last_completed_at = completed_at
                    if isinstance(step_result, dict):
                        self._state.steps[key].last_result = copy.deepcopy(step_result)
                    else:
                        self._state.steps[key].last_result = {"value": jsonable(step_result)}
        return result


class MaintenanceSidecarProcess:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle = None

    def _project_root(self):
        return self._settings.serving_db.parents[1]

    def _pid_file(self) -> Path:
        return self._project_root() / ".maintenance-sidecar.pid"

    def _read_pid(self) -> int | None:
        pid_file = self._pid_file()
        if not pid_file.exists():
            return None
        try:
            value = pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            pid = int(value)
        except ValueError:
            return None
        return pid if _pid_alive(pid) else None

    def _write_pid(self, pid: int) -> None:
        try:
            self._pid_file().write_text(str(pid), encoding="utf-8")
        except OSError:
            pass

    def _clear_pid(self, *, expected_pid: int | None = None) -> None:
        pid_file = self._pid_file()
        if not pid_file.exists():
            return
        if expected_pid is not None:
            try:
                current = int(pid_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                current = None
            if current is not None and current != expected_pid:
                return
        try:
            pid_file.unlink()
        except OSError:
            pass

    def _command(self) -> list[str]:
        project_root = self._project_root()
        return [
            sys.executable,
            str(project_root / "scripts" / "maintenance_sidecar.py"),
            "--sleep-seconds",
            str(self._settings.sidecar_sleep_seconds),
            "--parse-limit",
            str(self._settings.sidecar_parse_limit),
            "--eval-limit",
            str(self._settings.sidecar_eval_limit),
            "--ballchasing-count",
            str(self._settings.sidecar_ballchasing_count),
            "--youtube-limit",
            str(self._settings.sidecar_youtube_limit),
            "--index-every",
            str(self._settings.sidecar_index_every),
            "--ballchasing-every",
            str(self._settings.sidecar_ballchasing_every),
            "--carball-every",
            str(self._settings.sidecar_carball_every),
            "--eval-every",
            str(self._settings.sidecar_eval_every),
            "--youtube-every",
            str(self._settings.sidecar_youtube_every),
            "--live-every",
            str(self._settings.sidecar_live_every),
        ]

    def start(self) -> None:
        if not self._settings.sidecar_enabled:
            return
        with self._lock:
            if self._process and self._process.poll() is None:
                return
            existing_pid = self._read_pid()
            if existing_pid:
                return
            self._clear_pid()
            project_root = self._project_root()
            log_path = project_root / "maintenance-sidecar.log"
            self._log_handle = open(log_path, "ab")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._process = subprocess.Popen(
                self._command(),
                cwd=str(project_root),
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            self._write_pid(self._process.pid)

    def stop(self) -> None:
        with self._lock:
            process = self._process
            process_pid = process.pid if process else None
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
            self._process = None
            self._clear_pid(expected_pid=process_pid)
            if self._log_handle:
                self._log_handle.close()
                self._log_handle = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            running_pid = process.pid if process and process.poll() is None else self._read_pid()
            return {
                "enabled": bool(self._settings.sidecar_enabled),
                "running": bool(running_pid and _pid_alive(running_pid)),
                "pid": running_pid if running_pid and _pid_alive(running_pid) else None,
                "command": self._command(),
                "sleep_seconds": self._settings.sidecar_sleep_seconds,
                "parse_limit": self._settings.sidecar_parse_limit,
                "eval_limit": self._settings.sidecar_eval_limit,
                "ballchasing_count": self._settings.sidecar_ballchasing_count,
            }
