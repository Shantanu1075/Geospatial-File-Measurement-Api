"""File endpoints: upload, metadata, measurements, features.

The routes stay thin: HTTP concerns (status codes, response models, request
parsing) live here; all validation and GIS processing is delegated to
:class:`FileService`.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, File, UploadFile

from app.core.config import settings
from app.models.file import FileRepository
from app.schemas.file import (
    ErrorResponse,
    FileInfo,
    FeaturesResponse,
    MeasurementsResponse,
)
from app.services.crs_service import CrsService
from app.services.file_service import FileService
from app.services.geospatial_service import GeospatialService
from app.services.measurement_service import MeasurementService

router = APIRouter(
    prefix="/api/files",
    tags=["files"],
    responses={
        400: {"model": ErrorResponse, "description": "Invalid or unsupported input"},
        404: {"model": ErrorResponse, "description": "File not found"},
        413: {"model": ErrorResponse, "description": "Uploaded file too large"},
        422: {"model": ErrorResponse, "description": "Request validation error"},
    },
)


@lru_cache(maxsize=1)
def get_file_service() -> FileService:
    """Build the singleton FileService (also used by tests for cleanup)."""
    return FileService(
        repository=FileRepository(settings.database_path),
        geospatial_service=GeospatialService(),
        crs_service=CrsService(),
        measurement_service=MeasurementService(),
        settings=settings,
    )


@router.post(
    "/",
    response_model=FileInfo,
    status_code=201,
    summary="Upload a geospatial file",
    description=(
        "Accepts `multipart/form-data` with a `file` field. Supported formats: "
        "`.kml` and `.zip` containing a Shapefile. The file is validated, "
        "stored, read with GeoPandas, reprojected to an appropriate "
        "metre-based CRS, measured, and persisted — all within the request. "
        "Returns 400 for unsupported/invalid input, 413 when too large."
    ),
)
async def upload_file(
    file: UploadFile = File(..., description="A `.kml` file or `.zip` Shapefile archive."),
) -> FileInfo:
    return await get_file_service().process_upload(file)


@router.get(
    "/{file_id}/",
    response_model=FileInfo,
    summary="Get file metadata",
    description="Returns id, filename, feature count, CRS, status and creation time.",
)
async def get_file(file_id: str) -> FileInfo:
    return get_file_service().get_file_info(file_id)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Get measurements for every feature",
    description=(
        "Polygon/MultiPolygon → area (m²), LineString/MultiLineString → "
        "length (m), Point/MultiPoint → `null`. Features that cannot be "
        "measured carry a `measurement_status` explaining why "
        "(EMPTY_GEOMETRY, UNSUPPORTED_GEOMETRY, INVALID_GEOMETRY)."
    ),
)
async def get_measurements(file_id: str) -> MeasurementsResponse:
    return get_file_service().get_measurements(file_id)


@router.get(
    "/{file_id}/features/",
    response_model=FeaturesResponse,
    summary="Get all extracted features",
    description="Returns every feature with its GeoJSON geometry, CRS and properties.",
)
async def get_features(file_id: str) -> FeaturesResponse:
    return get_file_service().get_features(file_id)
