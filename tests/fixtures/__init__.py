"""Programmatic test-dataset builders.

All fixtures are generated in-memory during test setup — no external GIS data
or manual setup is required. Coordinates use small squares around
San Francisco (lon ≈ -122, lat ≈ 37.5), comfortably inside UTM zone 10N, so
expected areas/lengths can be cross-checked with geodesic computations.
"""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from typing import Any

import geopandas as gpd
from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)

# --------------------------------------------------------------------- shapes

# 0.01° × 0.01° square around (-122.0, 37.5): ~1.11 km × 0.88 km.
SQUARE_POLYGON = Polygon(
    [
        (-122.005, 37.495),
        (-121.995, 37.495),
        (-121.995, 37.505),
        (-122.005, 37.505),
        (-122.005, 37.495),
    ]
)

# 0.008° of longitude along lat 37.5: ~708 m.
SAMPLE_LINE = LineString(
    [(-122.004, 37.5), (-121.996, 37.5)]
)

SAMPLE_POINT = Point(-122.0, 37.5)

# Self-intersecting "bowtie" polygon (invalid until repaired).
BOWTIE_POLYGON = Polygon(
    [(-122.002, 37.498), (-121.998, 37.502), (-121.998, 37.498), (-122.002, 37.502), (-122.002, 37.498)]
)


def mixed_geometry_gdf() -> gpd.GeoDataFrame:
    """Polygon + LineString + Point with attributes, in EPSG:4326.

    KML (and GeoJSON) support mixed geometry types in one dataset; shapefiles
    do not, so this builder is for KML tests — use ``polygon_gdf`` etc. for
    shapefile fixtures.
    """
    return gpd.GeoDataFrame(
        {
            "name": ["Survey Area", "Transect", "Marker"],
            "category": ["test", "test", "test"],
        },
        geometry=[SQUARE_POLYGON, SAMPLE_LINE, SAMPLE_POINT],
        crs="EPSG:4326",
    )


def polygon_gdf(geometry: Polygon | MultiPolygon | None = None, crs: str | None = "EPSG:4326") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["Survey Area"], "category": ["test"]},
        geometry=[geometry or SQUARE_POLYGON],
        crs=crs,
    )


def multipolygon_gdf() -> gpd.GeoDataFrame:
    two_squares = MultiPolygon(
        [
            Polygon([(-122.01, 37.49), (-122.005, 37.49), (-122.005, 37.495), (-122.01, 37.495), (-122.01, 37.49)]),
            Polygon([(-121.995, 37.505), (-121.99, 37.505), (-121.99, 37.51), (-121.995, 37.51), (-121.995, 37.505)]),
        ]
    )
    return gpd.GeoDataFrame({"name": ["Two blocks"]}, geometry=[two_squares], crs="EPSG:4326")


def bowtie_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": ["Bowtie"]}, geometry=[BOWTIE_POLYGON], crs="EPSG:4326")


# ----------------------------------------------------------------------- KML

SAMPLE_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Test Survey</name>
    <Placemark>
      <name>Survey Area</name>
      <description>area fixture</description>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              -122.005,37.495,0 -121.995,37.495,0 -121.995,37.505,0 -122.005,37.505,0 -122.005,37.495,0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
    <Placemark>
      <name>Transect</name>
      <description>length fixture</description>
      <LineString>
        <coordinates>-122.004,37.5,0 -121.996,37.5,0</coordinates>
      </LineString>
    </Placemark>
    <Placemark>
      <name>Marker</name>
      <description>point fixture</description>
      <Point>
        <coordinates>-122.0,37.5,0</coordinates>
      </Point>
    </Placemark>
  </Document>
</kml>
"""


def kml_bytes(content: str = SAMPLE_KML) -> bytes:
    return content.encode("utf-8")


# ------------------------------------------------------------------- shapefiles

def shapefile_zip_bytes(
    gdf: gpd.GeoDataFrame,
    *,
    include_prj: bool = True,
    basename: str = "sample",
) -> bytes:
    """Write a GeoDataFrame as a Shapefile and zip its sidecar files."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        shp_path = os.path.join(tmp_dir, f"{basename}.shp")
        gdf.to_file(shp_path)
        if not include_prj:
            prj_path = os.path.join(tmp_dir, f"{basename}.prj")
            if os.path.exists(prj_path):
                os.remove(prj_path)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in sorted(os.listdir(tmp_dir)):
                archive.write(os.path.join(tmp_dir, entry), entry)
        return buffer.getvalue()


# ------------------------------------------------------------------------ zips

def zip_with_files(entries: dict[str, bytes]) -> bytes:
    """Build a ZIP from a {name: content} mapping (order preserved)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def zip_without_shapefile() -> bytes:
    return zip_with_files({"readme.txt": b"no shapefile here", "data.csv": b"a,b\n1,2\n"})


def malicious_traversal_zip() -> bytes:
    """ZIP whose entry tries to escape the extraction directory."""
    return zip_with_files({"../../evil.shp": b"malicious payload"})


def malicious_absolute_zip() -> bytes:
    """ZIP with an absolute-path entry."""
    return zip_with_files({"/tmp/evil.shp": b"malicious payload"})


def malicious_symlink_zip() -> bytes:
    """ZIP containing a symlink member (rejected outright)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("link.shp")
        info.create_system = 3  # unix
        info.external_attr = (0o120777 << 16)  # S_IFLNK | 0777
        archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


def upload_multipart(filename: str, content: bytes, content_type: str = "application/octet-stream") -> dict[str, Any]:
    """Payload fragment for httpx's files= parameter."""
    return {"file": (filename, content, content_type)}
