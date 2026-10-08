"""Upload orchestration: validation → storage → processing → persistence.

The API route delegates all work here. Processing runs synchronously inside
the upload request (kept off the event loop via a thread pool), which is
plenty for a service of this scale and avoids background-worker infrastructure.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.exceptions import (
    AppError,
    EmptyUploadError,
    FileNotProcessedError,
    FileNotFound,
    FileTooLargeError,
    ProcessingError,
    UnsupportedFileTypeError,
)
from app.models.file import FileRecord, FileRepository, FileStatus
from app.schemas.file import (
    FileInfo,
    FeaturesResponse,
    MeasurementsResponse,
)
from app.services.crs_service import CrsService
from app.services.geospatial_service import GeospatialService
from app.services.measurement_service import MeasurementService
from app.utils.file_utils import (
    file_extension,
    safe_extract_zip,
    sanitize_filename,
    save_upload_bytes,
)

logger = logging.getLogger(__name__)


class FileService:
    """Coordinates the whole upload pipeline and query endpoints."""

    def __init__(
        self,
        *,
        repository: FileRepository,
        geospatial_service: GeospatialService,
        crs_service: CrsService,
        measurement_service: MeasurementService,
        settings: Settings,
    ) -> None:
        self.repository = repository
        self.geospatial = geospatial_service
        self.crs = crs_service
        self.measurement = measurement_service
        self.settings = settings

    # ------------------------------------------------------------------ upload

    async def process_upload(self, upload: UploadFile) -> FileInfo:
        """Validate, store, process and persist an uploaded dataset.

        Raises AppError subclasses which the API layer maps to HTTP statuses
        (400/413 for validation, 400 for processing input problems, 500 for
        unexpected failures). Once a record exists, failures mark it FAILED so
        the metadata endpoint can report them.
        """
        original_name = sanitize_filename(upload.filename)
        extension = file_extension(original_name)
        logger.info("File upload started: %r (ext=%r)", original_name, extension or "-")

        if extension not in self.settings.allowed_extensions:
            raise UnsupportedFileTypeError()

        # Read at most max+1 bytes so oversized uploads fail fast with 413.
        max_bytes = self.settings.max_upload_bytes
        data = await upload.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise FileTooLargeError(
                f"The uploaded file exceeds the maximum allowed size of "
                f"{self.settings.max_upload_size_mb} MB."
            )
        if not data:
            raise EmptyUploadError()

        file_id = uuid.uuid4().hex
        storage_path = save_upload_bytes(
            data, self.settings.upload_dir, file_id, extension
        )

        record = self.repository.create(
            file_id=file_id,
            filename=storage_path.name,
            original_filename=original_name,
            storage_path=str(storage_path),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        try:
            # CPU-bound GIS work runs in a worker thread to keep the event
            # loop responsive; processing is synchronous with the request.
            await run_in_threadpool(self._process, record, extension)
        except AppError as exc:
            self.repository.mark_failed(record.id, exc.detail)
            logger.warning("Processing failed for %s: %s", record.id, exc.detail)
            raise
        except Exception as exc:
            self.repository.mark_failed(record.id, "Unexpected processing error.")
            logger.exception("Unexpected processing failure for %s", record.id)
            raise ProcessingError() from exc

        completed = self.repository.get(record.id)
        assert completed is not None
        logger.info(
            "Processing completed for %s: %d features",
            completed.id,
            completed.feature_count or 0,
        )
        return _to_file_info(completed)

    def _process(self, record: FileRecord, extension: str) -> None:
        """Run inside a worker thread: unpack (if needed) and analyze."""
        storage_path = Path(record.storage_path)
        if extension == ".zip":
            # Temporary directory is always cleaned up by the context manager,
            # so archives are never extracted into the uploads directory.
            with tempfile.TemporaryDirectory(prefix="geospatial-api-") as tmp_dir:
                shp_path = safe_extract_zip(
                    storage_path,
                    Path(tmp_dir),
                    max_entries=self.settings.max_zip_entries,
                    max_expanded_bytes=self.settings.max_zip_expanded_bytes,
                )
                self._analyze(record.id, shp_path, kind="shapefile")
        else:
            self._analyze(record.id, storage_path, kind="kml")

    def _analyze(self, record_id: str, path: Path, *, kind: str) -> None:
        """Read → require CRS → select/project CRS → measure → persist."""
        gdf = self.geospatial.read_dataset(path, kind)  # type: ignore[arg-type]
        source_crs = self.crs.require_crs(gdf)
        crs_label = source_crs.to_string()

        measurable = gdf.geometry.notna().any() if len(gdf) else False
        bounds = tuple(gdf.total_bounds) if measurable else None
        if bounds is not None and not np.isfinite(bounds).all():
            measurable = False
            bounds = None

        if measurable:
            assert bounds is not None
            target_crs = self.crs.select_measurement_crs(source_crs, bounds)
            projected = self.crs.transform_to(gdf, target_crs)
        else:
            # Nothing with coordinates to transform; skip CRS selection but
            # still report the dataset CRS (already validated above).
            logger.info("No measurable geometries in %s; skipping projection.", path.name)
            projected = gdf

        features = self.geospatial.extract_features(gdf, crs_label)
        measurements = self.measurement.measure_features(projected)

        self.repository.mark_completed(
            record_id,
            crs=crs_label,
            feature_count=len(features),
            features=features,
            measurements=measurements,
        )

    # ------------------------------------------------------------------ query

    def get_file_info(self, file_id: str) -> FileInfo:
        return _to_file_info(self._require_record(file_id))

    def get_measurements(self, file_id: str) -> MeasurementsResponse:
        record = self._require_completed(file_id)
        return MeasurementsResponse(
            file_id=file_id, measurements=record.measurements or []
        )

    def get_features(self, file_id: str) -> FeaturesResponse:
        record = self._require_completed(file_id)
        return FeaturesResponse(file_id=file_id, features=record.features or [])

    # ------------------------------------------------------------------ helpers

    def _require_record(self, file_id: str) -> FileRecord:
        record = self.repository.get(file_id)
        if record is None:
            raise FileNotFound()
        return record

    def _require_completed(self, file_id: str) -> FileRecord:
        record = self._require_record(file_id)
        if record.status is not FileStatus.COMPLETED:
            detail = record.error_message or (
                f"The file has not been processed successfully "
                f"(status: {record.status.value})."
            )
            raise FileNotProcessedError(detail)
        return record


def _to_file_info(record: FileRecord) -> FileInfo:
    return FileInfo(
        id=record.id,
        filename=record.original_filename,
        feature_count=record.feature_count,
        crs=record.crs,
        status=record.status.value,
        created_at=record.created_at,
        error=record.error_message,
    )
