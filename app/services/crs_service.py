"""CRS detection, projected-CRS selection and geometry transformation.

This is the heart of correct measurement in this service. Area and length must
never be computed on geographic (degree-based) coordinates such as
EPSG:4326, so this module decides *which* metre-based CRS to measure in:

1. Detect the source CRS (missing CRS is an explicit 400 error).
2. If it is already projected in metres → measure as-is (no needless
   transformation).
3. Otherwise (geographic, or projected in feet/us-ft/...) → pick the UTM zone
   containing the dataset centroid via ``pyproj.database.query_utm_crs_info``.
4. Where UTM does not exist (poles), fall back to a local azimuthal
   equidistant (AEQD) projection centred on the dataset — still metre-based.
5. Transform the geometries once, then measure.

Web Mercator (EPSG:3857) is deliberately *not* used as a measurement CRS:
its scale distortion grows with latitude (area is inflated by 1/cos²(lat),
≈1.6× at 37°N), which makes area and distance measurements wrong.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

import geopandas as gpd
from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info

from app.core.exceptions import CoordinateError, DatasetReadError, MissingCRSError

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

_METRIC_UNITS = {"metre", "metres", "meter", "meters", "m"}
# Cylindrical Mercator projections are metric but measure badly: scale grows
# as 1/cos(lat), inflating area by 1/cos²(lat) (≈1.6× at 37°N, ≈4× at 60°N).
# They are therefore excluded from "measured as-is" and fall through to the
# local UTM selection instead.
_MEASUREMENT_UNSAFE_EPSG = {3857, 3395}  # Web Mercator, World Mercator
# Half-size of the box (degrees) around the centroid used for the UTM query.
_CENTROID_AOI_DEG = 0.001


def is_metric_crs(crs: CRS) -> bool:
    """True when every axis of the CRS is expressed in metres."""
    if not crs.axis_info:
        return False
    return all(axis.unit_name.lower() in _METRIC_UNITS for axis in crs.axis_info)


class CrsService:
    """Detects CRSs and selects the metre-based CRS used for measurement."""

    def require_crs(self, gdf: gpd.GeoDataFrame) -> CRS:
        """Return the dataset CRS or fail with a clear 400 error.

        The API never silently assumes EPSG:4326 for datasets without a CRS.
        """
        if gdf.crs is None:
            raise MissingCRSError()
        return CRS.from_user_input(gdf.crs)

    def select_measurement_crs(
        self, source_crs: CRS, bounds: tuple[float, float, float, float]
    ) -> CRS:
        """Choose the CRS measurements will be computed in (always metres).

        ``bounds`` are the dataset bounds in the *source* CRS.
        """
        if (
            not source_crs.is_geographic
            and is_metric_crs(source_crs)
            and source_crs.to_epsg() not in _MEASUREMENT_UNSAFE_EPSG
        ):
            logger.info(
                "Source CRS %s is already projected in metres; no transformation needed.",
                source_crs.to_string(),
            )
            return source_crs
        if source_crs.to_epsg() in _MEASUREMENT_UNSAFE_EPSG:
            logger.info(
                "Source CRS %s is a Mercator projection that distorts measurements; "
                "selecting a local UTM zone instead.",
                source_crs.to_string(),
            )

        lon, lat = self._centroid_lon_lat(source_crs, bounds)

        utm = self._utm_crs_for(lon, lat)
        if utm is not None:
            logger.info(
                "Selected %s for measurement (centroid %.4f, %.4f).",
                utm.to_string(),
                lon,
                lat,
            )
            return utm

        # No UTM zone covers the centroid (poles) — use a local metre-based
        # projection centred on the data instead of a wrong default.
        fallback = CRS.from_proj4(
            f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs"
        )
        logger.info(
            "No UTM zone covers centroid %.4f, %.4f; using local AEQD fallback.",
            lon,
            lat,
        )
        return fallback

    def transform_to(self, gdf: gpd.GeoDataFrame, target_crs: CRS) -> gpd.GeoDataFrame:
        """Reproject a GeoDataFrame, returning it unchanged when not needed."""
        if gdf.crs is None:  # defensive: require_crs should have run already
            raise MissingCRSError()
        if CRS.from_user_input(gdf.crs) == target_crs:
            return gdf
        logger.info("Transforming dataset from %s to %s.", gdf.crs, target_crs.to_string())
        try:
            return gdf.to_crs(target_crs)
        except Exception as exc:
            logger.warning("CRS transformation failed: %s", exc, exc_info=True)
            raise DatasetReadError(
                "The dataset could not be transformed to a projected CRS."
            ) from exc

    def _centroid_lon_lat(
        self, source_crs: CRS, bounds: tuple[float, float, float, float]
    ) -> tuple[float, float]:
        """Centroid of ``bounds`` expressed as WGS84 longitude/latitude."""
        min_x, min_y, max_x, max_y = bounds
        if not all(math.isfinite(v) for v in (min_x, min_y, max_x, max_y)):
            raise CoordinateError()

        centre_x = (min_x + max_x) / 2.0
        centre_y = (min_y + max_y) / 2.0

        if source_crs.is_geographic:
            lon, lat = centre_x, centre_y
            # Antimeridian-spanning data: shift negatives by +360 so the
            # centre does not end up on the wrong side of the planet.
            if max_x - min_x > 180.0:
                shifted = [
                    v if v >= 0 else v + 360.0 for v in (min_x, max_x)
                ]
                lon = (shifted[0] + shifted[1]) / 2.0
        else:
            try:
                transformer = Transformer.from_crs(
                    source_crs, CRS.from_epsg(4326), always_xy=True
                )
                lon, lat = transformer.transform(centre_x, centre_y)
            except Exception as exc:
                logger.warning("Centroid transform failed: %s", exc, exc_info=True)
                raise CoordinateError() from exc

        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise CoordinateError()

        # Normalise into valid WGS84 ranges; clamp rather than fail so a few
        # bad vertices don't invalidate the whole file (per-feature garbage
        # still degrades gracefully to INVALID_GEOMETRY later).
        lon = ((lon + 180.0) % 360.0) - 180.0
        lat = max(-90.0, min(90.0, lat))
        return lon, lat

    @staticmethod
    def _utm_crs_for(lon: float, lat: float) -> CRS | None:
        """UTM zone CRS covering the given point, or None (e.g. polar)."""
        aoi = AreaOfInterest(
            west_lon_degree=max(-180.0, lon - _CENTROID_AOI_DEG),
            south_lat_degree=max(-90.0, lat - _CENTROID_AOI_DEG),
            east_lon_degree=min(180.0, lon + _CENTROID_AOI_DEG),
            north_lat_degree=min(90.0, lat + _CENTROID_AOI_DEG),
        )
        try:
            infos = query_utm_crs_info(datum_name="WGS 84", area_of_interest=aoi)
        except Exception as exc:  # pragma: no cover - pyproj internal failure
            logger.warning("UTM query failed: %s", exc, exc_info=True)
            return None
        if not infos:
            return None
        return CRS.from_user_input(f"{infos[0].auth_name}:{infos[0].code}")


def centroid_bounds(geometry: BaseGeometry | None) -> tuple[float, float, float, float] | None:
    """Bounds of a geometry when they are finite, else None (helper for tests)."""
    if geometry is None or geometry.is_empty:
        return None
    min_x, min_y, max_x, max_y = geometry.bounds
    if not all(math.isfinite(v) for v in (min_x, min_y, max_x, max_y)):
        return None
    return (min_x, min_y, max_x, max_y)
