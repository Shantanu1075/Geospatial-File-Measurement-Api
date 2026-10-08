"""Reading geospatial datasets (KML / Shapefile) and extracting features.

All driver-level I/O lives here: GeoPandas/pyogrio read the file, this service
turns the resulting GeoDataFrame into JSON-safe feature dictionaries
(feature id, geometry type, GeoJSON geometry, CRS, properties).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd

from app.core.exceptions import DatasetReadError, KMLParseError
from app.utils.file_utils import geometry_to_geojson, json_safe

logger = logging.getLogger(__name__)

DatasetKind = Literal["kml", "shapefile"]


class GeospatialService:
    """Read datasets and extract GeoJSON-compatible feature data."""

    def read_dataset(self, path: Path, kind: DatasetKind) -> gpd.GeoDataFrame:
        """Read a KML file or a ``.shp`` into a GeoDataFrame.

        Raises :class:`KMLParseError` for unreadable KML and
        :class:`DatasetReadError` for any other read failure — internal driver
        exceptions never leak to API clients.
        """
        logger.info("Reading %s dataset: %s", kind, path.name)
        try:
            gdf = gpd.read_file(path)
        except Exception as exc:
            logger.warning(
                "Failed to read %s dataset %s: %s", kind, path.name, exc, exc_info=True
            )
            if kind == "kml":
                raise KMLParseError() from exc
            raise DatasetReadError() from exc

        # The KML specification mandates WGS84 lon/lat coordinates, so if the
        # driver reports no CRS for a valid KML, assigning EPSG:4326 follows
        # the format definition — it is not a silent assumption. Shapefiles
        # without a .prj keep crs=None and fail later with MissingCRSError.
        if gdf.crs is None and kind == "kml":
            logger.info("KML driver reported no CRS; assigning EPSG:4326 per KML spec.")
            gdf = gdf.set_crs("EPSG:4326")

        logger.info("Loaded %d features from %s dataset.", len(gdf), kind)
        return gdf

    def extract_features(self, gdf: gpd.GeoDataFrame, crs_label: str) -> list[dict[str, Any]]:
        """Extract per-feature dicts: id, type, GeoJSON geometry, CRS, properties.

        Null geometries become ``geometry: None`` with geometry type ``Null``;
        attribute values are converted to JSON-safe primitives and fully-null
        columns (common in KML) are dropped to keep responses readable.
        """
        geometry_col = gdf.geometry.name
        attribute_cols = [col for col in gdf.columns if col != geometry_col]

        features: list[dict[str, Any]] = []
        for position, geom in enumerate(gdf.geometry):
            row = gdf.iloc[position]
            properties = {
                str(col): json_safe(row[col]) for col in attribute_cols
            }
            properties = {k: v for k, v in properties.items() if v is not None}
            features.append(
                {
                    "feature_id": position,
                    "geometry_type": geom.geom_type if geom is not None else "Null",
                    "geometry": geometry_to_geojson(geom),
                    "crs": crs_label,
                    "properties": properties,
                }
            )
        return features
