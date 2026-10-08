"""Feature-level measurements (area/length) and measurement statuses.

Assumes geometries are already expressed in a metre-based projected CRS (the
CRS service guarantees this) — values are therefore directly in m² / m.

Geometry handling rules:
- Polygon / MultiPolygon   → area in m²
- LineString / MultiLineString / LinearRing → length in m
- Point / MultiPoint       → no measurement (``measurement: null``)
- anything else            → ``UNSUPPORTED_GEOMETRY``
- null / empty geometry    → ``EMPTY_GEOMETRY``
- invalid geometry         → conservative repair; when repair fails,
  ``INVALID_GEOMETRY``

A single unmeasurable feature never fails the whole file: statuses are
returned per feature and processing continues.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from shapely import make_valid
from shapely.geometry.base import BaseGeometry

from app.schemas.file import Measurement

logger = logging.getLogger(__name__)

AREA_TYPES = frozenset({"Polygon", "MultiPolygon"})
LENGTH_TYPES = frozenset({"LineString", "MultiLineString", "LinearRing"})
# Geometry types that carry no measurable dimension.
DIMENSIONLESS_TYPES = frozenset({"Point", "MultiPoint"})

STATUS_EMPTY = "EMPTY_GEOMETRY"
STATUS_UNSUPPORTED = "UNSUPPORTED_GEOMETRY"
STATUS_INVALID = "INVALID_GEOMETRY"


def _repair(geom: BaseGeometry) -> BaseGeometry | None:
    """Try to repair an invalid geometry; return None when unsuccessful.

    Prefers ``make_valid`` (topology-preserving) and falls back to the classic
    ``buffer(0)`` trick. Repair is only attempted for geometries that are
    actually invalid — valid geometries are never touched.
    """
    for attempt in (make_valid, lambda g: g.buffer(0)):
        try:
            candidate = attempt(geom)
        except Exception as exc:  # GEOS may raise on pathological input
            logger.debug("Repair attempt failed: %s", exc)
            continue
        if candidate is not None and not candidate.is_empty and candidate.is_valid:
            return candidate
    return None


class MeasurementService:
    """Compute measurements for features with graceful per-feature statuses."""

    def measure_geometry(
        self, geom: BaseGeometry | None
    ) -> tuple[Measurement | None, str | None]:
        """Return ``(measurement, status)`` for one geometry.

        Exactly one of the two is non-None (both None means "no measurement
        required", i.e. point geometries).
        """
        if geom is None or geom.is_empty:
            return None, STATUS_EMPTY

        geom_type = geom.geom_type
        if geom_type in DIMENSIONLESS_TYPES:
            return None, None
        if geom_type in AREA_TYPES:
            kind, unit = "area", "m²"
        elif geom_type in LENGTH_TYPES:
            kind, unit = "length", "m"
        else:
            return None, STATUS_UNSUPPORTED

        if not geom.is_valid:
            repaired = _repair(geom)
            if repaired is None:
                logger.info("Invalid %s geometry could not be repaired.", geom_type)
                return None, STATUS_INVALID
            geom = repaired

        value = float(geom.area if kind == "area" else geom.length)
        if not math.isfinite(value):
            return None, STATUS_INVALID
        return Measurement(type=kind, value=value, unit=unit), None

    def measure_features(self, gdf: Any) -> list[dict[str, Any]]:
        """Measure every feature of a (projected) GeoDataFrame.

        Never raises for individual bad features — a warning is logged and the
        feature receives an ``INVALID_GEOMETRY`` status instead.
        """
        results: list[dict[str, Any]] = []
        for position, geom in enumerate(gdf.geometry):
            try:
                measurement, status = self.measure_geometry(geom)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Measurement failed for feature %d: %s", position, exc, exc_info=True
                )
                measurement, status = None, STATUS_INVALID
            results.append(
                {
                    "feature_id": position,
                    "geometry_type": geom.geom_type if geom is not None else "Null",
                    "measurement": measurement.model_dump() if measurement else None,
                    "measurement_status": status,
                }
            )
        return results
