"""Pydantic response models for the API layer."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Measurement(BaseModel):
    """A single scalar measurement, always in metre-based units."""

    type: Literal["area", "length"]
    value: float = Field(..., description="Measured value expressed in `unit`.")
    unit: Literal["m²", "m"]


class FeatureMeasurement(BaseModel):
    """Measurement result for one feature."""

    feature_id: int
    geometry_type: str
    measurement: Measurement | None = Field(
        default=None,
        description=(
            "Null for point geometries, unsupported geometry types, "
            "empty/null geometries, and invalid geometries that could not be repaired."
        ),
    )
    measurement_status: str | None = Field(
        default=None,
        description=(
            "Why no measurement is available: EMPTY_GEOMETRY, "
            "UNSUPPORTED_GEOMETRY or INVALID_GEOMETRY. Null when a "
            "measurement exists or when the geometry type simply has no "
            "measurement (Point/MultiPoint)."
        ),
    )


class GeoJSONGeometry(BaseModel):
    """GeoJSON geometry object (RFC 7946)."""

    type: str
    coordinates: Any | None = None
    geometries: list["GeoJSONGeometry"] | None = Field(
        default=None, description="Only present for GeometryCollection."
    )


class Feature(BaseModel):
    """One extracted feature of an uploaded dataset."""

    feature_id: int
    geometry_type: str
    geometry: GeoJSONGeometry | None
    crs: str
    properties: dict[str, Any]


class FileInfo(BaseModel):
    """Metadata for an uploaded file."""

    id: str
    filename: str = Field(
        ..., description="Sanitized original filename supplied by the client."
    )
    feature_count: int | None = None
    crs: str | None = Field(default=None, description="Source CRS, e.g. EPSG:4326.")
    status: Literal["PROCESSING", "COMPLETED", "FAILED"]
    created_at: datetime
    error: str | None = Field(
        default=None, description="Processing error message when status is FAILED."
    )


class MeasurementsResponse(BaseModel):
    """Measurements for every feature of a processed file."""

    file_id: str
    measurements: list[FeatureMeasurement]


class FeaturesResponse(BaseModel):
    """All extracted features of a processed file."""

    file_id: str
    features: list[Feature]


class ErrorResponse(BaseModel):
    """Consistent error body returned by every failing endpoint."""

    detail: str
