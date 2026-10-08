"""SQLite persistence for file metadata, processing status and results.

A single ``files`` table is all this service needs: file id, names, storage
path, CRS, feature count, status, error message, plus the processed features
and measurements as JSON documents. SQLite keeps the deployment simple while
still surviving restarts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


class FileStatus(StrEnum):
    """Processing lifecycle of an uploaded file."""

    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(slots=True, frozen=True)
class FileRecord:
    """Row shape of the ``files`` table."""

    id: str
    filename: str
    original_filename: str
    storage_path: str
    status: FileStatus
    created_at: str
    crs: str | None = None
    feature_count: int | None = None
    error_message: str | None = None
    features: list[dict[str, Any]] | None = None
    measurements: list[dict[str, Any]] | None = None


class FileRepository:
    """Thin SQLite repository (one connection per operation).

    Opening a fresh connection per call keeps the repository safe to use from
    FastAPI's worker threads without managing a shared connection pool.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit on success, roll back on error
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id               TEXT PRIMARY KEY,
                    filename         TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    storage_path     TEXT NOT NULL,
                    crs              TEXT,
                    feature_count    INTEGER,
                    status           TEXT NOT NULL,
                    error_message    TEXT,
                    features_json    TEXT,
                    measurements_json TEXT,
                    created_at       TEXT NOT NULL
                )
                """
            )

    def create(
        self,
        *,
        file_id: str,
        filename: str,
        original_filename: str,
        storage_path: str,
        created_at: str,
    ) -> FileRecord:
        """Insert a new record in PROCESSING state."""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO files
                    (id, filename, original_filename, storage_path, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    filename,
                    original_filename,
                    storage_path,
                    FileStatus.PROCESSING.value,
                    created_at,
                ),
            )
        record = self.get(file_id)
        assert record is not None  # just inserted
        return record

    def get(self, file_id: str) -> FileRecord | None:
        """Fetch one record by id, or None when it does not exist."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        if row is None:
            return None
        return FileRecord(
            id=row["id"],
            filename=row["filename"],
            original_filename=row["original_filename"],
            storage_path=row["storage_path"],
            status=FileStatus(row["status"]),
            created_at=row["created_at"],
            crs=row["crs"],
            feature_count=row["feature_count"],
            error_message=row["error_message"],
            features=json.loads(row["features_json"]) if row["features_json"] else None,
            measurements=(
                json.loads(row["measurements_json"]) if row["measurements_json"] else None
            ),
        )

    def mark_completed(
        self,
        file_id: str,
        *,
        crs: str,
        feature_count: int,
        features: list[dict[str, Any]],
        measurements: list[dict[str, Any]],
    ) -> None:
        """Mark a record COMPLETED and store its processed results."""
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE files
                SET status = ?, crs = ?, feature_count = ?,
                    features_json = ?, measurements_json = ?, error_message = NULL
                WHERE id = ?
                """,
                (
                    FileStatus.COMPLETED.value,
                    crs,
                    feature_count,
                    json.dumps(features, ensure_ascii=False),
                    json.dumps(measurements, ensure_ascii=False),
                    file_id,
                ),
            )

    def mark_failed(self, file_id: str, error_message: str) -> None:
        """Mark a record FAILED with a client-safe error message."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE files SET status = ?, error_message = ? WHERE id = ?",
                (FileStatus.FAILED.value, error_message, file_id),
            )

    def delete_all(self) -> None:
        """Remove every record (used by the test suite for isolation)."""
        with self._connection() as conn:
            conn.execute("DELETE FROM files")
