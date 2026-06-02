from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import shutil
import time
from typing import Any, Iterable, Iterator

import duckdb

from .config import get_settings


def replica_db_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.stem}_read{db_path.suffix}")


def preferred_read_db_path(db_path: Path) -> tuple[Path, Path | None]:
    replica = replica_db_path(db_path)
    if replica.exists():
        return replica, db_path
    return db_path, None


def refresh_read_replica(db_path: Path | None = None) -> Path | None:
    settings = get_settings()
    source = Path(db_path or settings.serving_db)
    if not source.exists():
        return None
    replica = replica_db_path(source)
    temp_replica = replica.with_suffix(f"{replica.suffix}.tmp")
    replica.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(6):
        try:
            shutil.copy2(source, temp_replica)
            temp_replica.replace(replica)
            return replica
        except OSError:
            if attempt == 5:
                return None
            time.sleep(min(0.5, 0.05 * (2 ** attempt)))
    return replica


def _connect_with_retry(
    db_path: Path,
    *,
    read_only: bool,
    fallback_path: Path | None = None,
) -> duckdb.DuckDBPyConnection:
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            return duckdb.connect(str(db_path), read_only=read_only)
        except duckdb.ConnectionException as exc:
            last_error = exc
            if read_only and "different configuration" in str(exc).lower():
                return duckdb.connect(str(db_path), read_only=False)
            if attempt == 5:
                raise
            time.sleep(min(0.5, 0.05 * (2 ** attempt)))
        except duckdb.IOException as exc:
            last_error = exc
            if attempt == 5:
                if read_only and fallback_path and fallback_path.exists():
                    return duckdb.connect(str(fallback_path), read_only=True)
                raise
            time.sleep(min(0.5, 0.05 * (2 ** attempt)))
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Unable to connect to DuckDB at {db_path}")


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    if isinstance(value, bytes):
        return value.hex()
    return value


def rows_to_dicts(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description or []]
    return [
        {column: jsonable(value) for column, value in zip(columns, row)}
        for row in cursor.fetchall()
    ]


@contextmanager
def database_connection(
    db_path: Path | None = None,
    *,
    read_only: bool = True,
) -> Iterator[duckdb.DuckDBPyConnection]:
    settings = get_settings()
    resolved_path = Path(db_path or settings.serving_db)
    connect_path = resolved_path
    fallback_path: Path | None = None
    if read_only and not resolved_path.exists() and not replica_db_path(resolved_path).exists():
        raise FileNotFoundError(
            f"Database not found at {resolved_path}. Run scripts/build_warehouse.py first."
        )
    if read_only:
        connect_path, fallback_path = preferred_read_db_path(resolved_path)
    con = _connect_with_retry(connect_path, read_only=read_only, fallback_path=fallback_path)
    try:
        yield con
    finally:
        con.close()


@contextmanager
def serving_connection(read_only: bool = True) -> Iterator[duckdb.DuckDBPyConnection]:
    settings = get_settings()
    db_path = settings.serving_db
    if read_only and not db_path.exists():
        raise FileNotFoundError(
            f"Serving database not found at {db_path}. Run scripts/build_warehouse.py first."
        )
    with database_connection(db_path, read_only=read_only) as con:
        yield con


def fetch_all(
    sql: str,
    params: Iterable[Any] | None = None,
    *,
    read_only: bool = True,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    if db_path is None:
        with serving_connection(read_only=read_only) as con:
            return rows_to_dicts(con.execute(sql, list(params or [])))
    with database_connection(db_path, read_only=read_only) as con:
        return rows_to_dicts(con.execute(sql, list(params or [])))


def fetch_one(
    sql: str,
    params: Iterable[Any] | None = None,
    *,
    read_only: bool = True,
) -> dict[str, Any] | None:
    rows = fetch_all(sql, params, read_only=read_only)
    return rows[0] if rows else None
