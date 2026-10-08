"""Measurement tests: areas, lengths, points, multi- and bad geometries.

Expected values are cross-checked against geodesic computations on the WGS84
ellipsoid with relative tolerances — no exact float equality on real-world
measurements.
"""

from __future__ import annotations

import math

import pytest
from pyproj import Geod
from shapely.geometry import GeometryCollection, LineString, Point, Polygon

from app.services.measurement_service import (
    STATUS_INVALID,
    STATUS_UNSUPPORTED,
    MeasurementService,
)
from tests.fixtures import (
    BOWTIE_POLYGON,
    SAMPLE_LINE,
    SQUARE_POLYGON,
    kml_bytes,
    bowtie_gdf,
    multipolygon_gdf,
    shapefile_zip_bytes,
    upload_multipart,
)

GEOD = Geod(ellps="WGS84")

service = MeasurementService()


async def _upload(client, filename: str, content: bytes) -> str:
    response = await client.post("/api/files/", files=upload_multipart(filename, content))
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _measurements(client, file_id: str) -> list[dict]:
    response = await client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["file_id"] == file_id
    return body["measurements"]


# ----------------------------------------------------------------- integration


async def test_mixed_kml_measurements(client):
    """Polygon → area, LineString → length, Point → null (via a KML upload)."""
    file_id = await _upload(client, "survey.kml", kml_bytes())
    measurements = await _measurements(client, file_id)
    assert len(measurements) == 3

    area_feature = measurements[0]
    assert area_feature["feature_id"] == 0
    assert area_feature["geometry_type"] == "Polygon"
    assert area_feature["measurement"]["type"] == "area"
    assert area_feature["measurement"]["unit"] == "m²"
    assert area_feature["measurement_status"] is None

    # Geodesic reference area for the 0.01°×0.01° square.
    expected_area = abs(GEOD.geometry_area_perimeter(SQUARE_POLYGON)[0])
    assert area_feature["measurement"]["value"] == pytest.approx(expected_area, rel=0.02)
    # Sanity: value is square-metres magnitude, not square-degrees.
    # 0.01° lat (≈1111 m) × 0.01° lon at 37.5°N (≈880 m) ≈ 0.98 km².
    assert 9e5 < area_feature["measurement"]["value"] < 1.1e6

    length_feature = measurements[1]
    assert length_feature["geometry_type"] == "LineString"
    assert length_feature["measurement"]["type"] == "length"
    assert length_feature["measurement"]["unit"] == "m"
    expected_length = GEOD.geometry_length(SAMPLE_LINE)
    assert length_feature["measurement"]["value"] == pytest.approx(expected_length, rel=0.01)
    assert 650 < length_feature["measurement"]["value"] < 780

    point_feature = measurements[2]
    assert point_feature["feature_id"] == 2
    assert point_feature["geometry_type"] == "Point"
    assert point_feature["measurement"] is None
    assert point_feature["measurement_status"] is None


async def test_shapefile_polygon_area(client):
    content = shapefile_zip_bytes(multipolygon_gdf())
    file_id = await _upload(client, "blocks.zip", content)
    measurements = await _measurements(client, file_id)
    assert len(measurements) == 1
    measurement = measurements[0]["measurement"]
    assert measurement["type"] == "area"
    assert measurement["unit"] == "m²"
    # Two ~555 m × 555 m blocks: expect roughly 0.6 km², order-of-magnitude check.
    assert 300_000 < measurement["value"] < 900_000


async def test_invalid_geometry_is_repaired_without_failing_file(client):
    """A bowtie polygon gets repaired (or flagged) — the file still succeeds."""
    content = shapefile_zip_bytes(bowtie_gdf())
    file_id = await _upload(client, "bowtie.zip", content)
    response = await client.get(f"/api/files/{file_id}/")
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"

    measurements = await _measurements(client, file_id)
    assert len(measurements) == 1
    first = measurements[0]
    # Either repaired successfully (valid area) or flagged INVALID_GEOMETRY.
    if first["measurement"] is None:
        assert first["measurement_status"] == STATUS_INVALID
    else:
        assert first["measurement"]["type"] == "area"
        assert first["measurement"]["value"] > 0


# ----------------------------------------------------------------------- units


def test_point_returns_no_measurement():
    measurement, status = service.measure_geometry(Point(-122.0, 37.5))
    assert measurement is None
    assert status is None


def test_linestring_returns_length_in_meters():
    # 300 m east + 400 m north in a projected (metre) CRS.
    line = LineString([(0, 0), (300, 0), (300, 400)])
    measurement, status = service.measure_geometry(line)
    assert status is None
    assert measurement is not None
    assert measurement.type == "length"
    assert measurement.unit == "m"
    assert measurement.value == pytest.approx(700.0, rel=1e-9)


def test_polygon_returns_area_in_square_meters():
    square = Polygon([(0, 0), (120, 0), (120, 80), (0, 80), (0, 0)])
    measurement, status = service.measure_geometry(square)
    assert status is None
    assert measurement is not None
    assert measurement.type == "area"
    assert measurement.unit == "m²"
    assert measurement.value == pytest.approx(9600.0, rel=1e-9)


def test_multipolygon_total_area():
    from shapely.geometry import MultiPolygon

    multi = MultiPolygon(
        [
            Polygon([(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]),
            Polygon([(200, 0), (250, 0), (250, 50), (200, 50), (200, 0)]),
        ]
    )
    measurement, status = service.measure_geometry(multi)
    assert status is None
    assert measurement is not None
    assert measurement.type == "area"
    assert measurement.value == pytest.approx(10_000 + 2_500, rel=1e-9)


def test_multilinestring_total_length():
    from shapely.geometry import MultiLineString

    multi = MultiLineString([[(0, 0), (100, 0)], [(0, 0), (0, 50)]])
    measurement, status = service.measure_geometry(multi)
    assert status is None
    assert measurement is not None
    assert measurement.type == "length"
    assert measurement.value == pytest.approx(150.0, rel=1e-9)


def test_multipoint_returns_no_measurement():
    from shapely.geometry import MultiPoint

    measurement, status = service.measure_geometry(MultiPoint([(0, 0), (1, 1)]))
    assert measurement is None
    assert status is None


def test_unsupported_geometry_returns_status_not_crash():
    collection = GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])])
    measurement, status = service.measure_geometry(collection)
    assert measurement is None
    assert status == STATUS_UNSUPPORTED


def test_null_and_empty_geometries_return_status():
    assert service.measure_geometry(None) == (None, "EMPTY_GEOMETRY")
    empty_polygon = Polygon()
    assert service.measure_geometry(empty_polygon) == (None, "EMPTY_GEOMETRY")


def test_unrepairable_invalid_geometry_returns_invalid_status():
    # NaN coordinates cannot be repaired — must not raise.
    broken = Polygon([(0, 0), (1, math.nan), (1, 1), (0, 0)])
    assert not broken.is_valid
    measurement, status = service.measure_geometry(broken)
    assert measurement is None
    assert status == STATUS_INVALID


def test_bowtie_geometry_is_repaired_to_valid_area():
    assert not BOWTIE_POLYGON.is_valid
    measurement, status = service.measure_geometry(BOWTIE_POLYGON)
    # Repair succeeds for a simple bowtie (degrees here, but value must exist).
    assert status is None
    assert measurement is not None
    assert measurement.type == "area"
    assert measurement.value > 0
