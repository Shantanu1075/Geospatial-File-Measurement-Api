"""Upload endpoint tests: accepted formats and every rejection path."""

from __future__ import annotations

import os

import pytest

from app.core.config import settings
from tests.fixtures import (
    kml_bytes,
    malicious_traversal_zip,
    polygon_gdf,
    shapefile_zip_bytes,
    upload_multipart,
    zip_without_shapefile,
)


async def test_kml_upload_succeeds(client):
    response = await client.post(
        "/api/files/", files=upload_multipart("survey.kml", kml_bytes())
    )
    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "survey.kml"
    assert body["status"] == "COMPLETED"
    assert body["feature_count"] == 3
    assert body["crs"] == "EPSG:4326"
    assert body["error"] is None
    assert body["id"]
    assert body["created_at"]


async def test_shapefile_zip_upload_succeeds(client):
    # Shapefiles store one geometry family per file; use a polygon dataset.
    content = shapefile_zip_bytes(polygon_gdf())
    response = await client.post(
        "/api/files/", files=upload_multipart("survey.zip", content)
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["feature_count"] == 1
    assert body["crs"] == "EPSG:4326"


async def test_unsupported_extension_rejected(client):
    response = await client.post(
        "/api/files/", files=upload_multipart("data.csv", b"a,b\n1,2\n")
    )
    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Unsupported file type. Only .kml and .zip Shapefiles are supported."
    )


async def test_empty_upload_rejected(client):
    response = await client.post(
        "/api/files/", files=upload_multipart("empty.kml", b"")
    )
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


async def test_invalid_zip_rejected(client):
    response = await client.post(
        "/api/files/", files=upload_multipart("broken.zip", b"this is not a zip archive")
    )
    assert response.status_code == 400
    assert "ZIP" in response.json()["detail"]


async def test_zip_without_shapefile_rejected(client):
    response = await client.post(
        "/api/files/", files=upload_multipart("nope.zip", zip_without_shapefile())
    )
    assert response.status_code == 400
    assert "does not contain a Shapefile" in response.json()["detail"]


async def test_invalid_kml_rejected(client):
    response = await client.post(
        "/api/files/",
        files=upload_multipart("broken.kml", b"<?xml version='1.0'?><not-kml>garbage"),
    )
    assert response.status_code == 400
    assert "KML" in response.json()["detail"]


async def test_missing_file_field_returns_422(client):
    response = await client.post("/api/files/")
    assert response.status_code == 422
    assert "detail" in response.json()


async def test_oversized_upload_rejected(client, monkeypatch):
    """Payload bytes over the cap fail with 413 before any processing."""
    monkeypatch.setattr(settings, "max_upload_size_mb", 1)
    oversized = os.urandom(1024 * 1024 + 512 * 1024)  # 1.5 MB of noise
    response = await client.post(
        "/api/files/", files=upload_multipart("huge.zip", oversized)
    )
    assert response.status_code == 413
    assert "maximum allowed size" in response.json()["detail"]


async def test_traversal_filename_is_neutralized(client):
    """A filename like ../../evil.kml must not escape the uploads directory."""
    response = await client.post(
        "/api/files/", files=upload_multipart("../../evil.kml", kml_bytes())
    )
    assert response.status_code == 201
    body = response.json()
    # Display name is sanitized: no directory parts survive.
    assert "/" not in body["filename"] and "\\" not in body["filename"]
    assert body["filename"].endswith(".kml")
    # The stored file lives inside the uploads directory under a UUID name.
    stored = list(settings.upload_dir.iterdir())
    assert len(stored) == 1
    assert stored[0].suffix == ".kml"
    assert stored[0].name == body["id"] + ".kml"


async def test_responses_do_not_expose_storage_paths(client):
    response = await client.post(
        "/api/files/", files=upload_multipart("survey.kml", kml_bytes())
    )
    assert response.status_code == 201
    text = response.text
    assert str(settings.upload_dir) not in text
    assert "storage_path" not in text
    assert str(settings.database_path) not in text


async def test_unsafe_zip_is_rejected(client):
    response = await client.post(
        "/api/files/", files=upload_multipart("evil.zip", malicious_traversal_zip())
    )
    assert response.status_code == 400
    assert "unsafe" in response.json()["detail"].lower()
    # Nothing escaped: only this test's uploads exist, no evil.shp anywhere.
    assert not any(p.name == "evil.shp" for p in settings.upload_dir.iterdir())


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("notes.txt", b"plain text"),
        ("archive.7z", b"7z"),
        ("shapefile.shp", b"raw shp not accepted directly"),
    ],
)
async def test_other_extensions_rejected(client, filename, content):
    response = await client.post("/api/files/", files=upload_multipart(filename, content))
    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]
