"""File metadata and features endpoint tests."""

from __future__ import annotations

import uuid

from tests.fixtures import kml_bytes, upload_multipart


async def _upload_sample(client, filename: str = "survey.kml") -> str:
    response = await client.post(
        "/api/files/", files=upload_multipart(filename, kml_bytes())
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_file_info_returns_metadata(client):
    file_id = await _upload_sample(client)
    response = await client.get(f"/api/files/{file_id}/")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == file_id
    assert body["filename"] == "survey.kml"
    assert body["feature_count"] == 3
    assert body["crs"] == "EPSG:4326"
    assert body["status"] == "COMPLETED"
    assert body["error"] is None
    # ISO-8601 timestamp
    assert body["created_at"].startswith("20")


async def test_unknown_file_id_returns_404(client):
    response = await client.get(f"/api/files/{uuid.uuid4().hex}/")
    assert response.status_code == 404
    assert "detail" in response.json()


async def test_unknown_measurements_return_404(client):
    response = await client.get(f"/api/files/{uuid.uuid4().hex}/measurements/")
    assert response.status_code == 404


async def test_unknown_features_return_404(client):
    response = await client.get(f"/api/files/{uuid.uuid4().hex}/features/")
    assert response.status_code == 404


async def test_features_endpoint_returns_geojson_features(client):
    file_id = await _upload_sample(client)
    response = await client.get(f"/api/files/{file_id}/features/")
    assert response.status_code == 200
    body = response.json()
    assert body["file_id"] == file_id
    features = body["features"]
    assert len(features) == 3

    polygon_feature = features[0]
    assert polygon_feature["feature_id"] == 0
    assert polygon_feature["geometry_type"] == "Polygon"
    assert polygon_feature["crs"] == "EPSG:4326"
    assert polygon_feature["geometry"]["type"] == "Polygon"
    assert polygon_feature["geometry"]["coordinates"]  # non-empty
    # GeoJSON coordinate pairs are plain arrays of numbers, not shapely/tuples.
    first_ring = polygon_feature["geometry"]["coordinates"][0]
    assert all(isinstance(pt, list) for pt in first_ring)

    # Properties are JSON-safe and include driver columns that hold values.
    properties = polygon_feature["properties"]
    assert isinstance(properties, dict)
    assert properties.get("Name") == "Survey Area"

    # Point feature survives with its own geometry type.
    assert features[2]["geometry_type"] == "Point"
    assert features[2]["geometry"]["type"] == "Point"


async def test_uploaded_files_are_persisted_on_disk(client):
    file_id = await _upload_sample(client)
    from app.api.files import get_file_service

    record = get_file_service().repository.get(file_id)
    assert record is not None
    assert record.status.value == "COMPLETED"
    assert record.features is not None and len(record.features) == 3
    assert record.measurements is not None and len(record.measurements) == 3
