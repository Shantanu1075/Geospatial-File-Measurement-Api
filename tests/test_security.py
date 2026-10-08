"""Security tests: malicious archives, zip-slip, bombs, and limits."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.utils.file_utils import sanitize_filename, safe_extract_zip
from app.core.exceptions import (
    InvalidArchiveError,
    MissingShapefileError,
    UnsafeArchiveError,
)
from tests.fixtures import (
    kml_bytes,
    malicious_absolute_zip,
    malicious_symlink_zip,
    malicious_traversal_zip,
    upload_multipart,
    zip_with_files,
)


# ---------------------------------------------------------------- via the API

async def test_zip_slip_entry_rejected(client):
    response = await client.post(
        "/api/files/", files=upload_multipart("evil.zip", malicious_traversal_zip())
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "unsafe" in detail.lower()
    # The rejection message must not echo internal paths.
    assert "/" not in detail or "only" in detail  # no server paths leaked


async def test_absolute_path_entry_rejected(client):
    response = await client.post(
        "/api/files/", files=upload_multipart("abs.zip", malicious_absolute_zip())
    )
    assert response.status_code == 400
    assert "unsafe" in response.json()["detail"].lower()


async def test_symlink_entry_rejected(client):
    response = await client.post(
        "/api/files/", files=upload_multipart("link.zip", malicious_symlink_zip())
    )
    assert response.status_code == 400
    assert "symbolic link" in response.json()["detail"]


async def test_zip_bomb_size_limit(client, monkeypatch):
    """Declared expanded size over the cap → rejected before extraction."""
    monkeypatch.setattr(settings, "max_zip_expanded_mb", 1)
    bomb = zip_with_files({"payload.bin": b"\x00" * (3 * 1024 * 1024)})
    response = await client.post("/api/files/", files=upload_multipart("bomb.zip", bomb))
    assert response.status_code == 400
    assert "expands beyond" in response.json()["detail"]


async def test_zip_entry_count_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "max_zip_entries", 3)
    many = zip_with_files({f"file{i}.txt": b"x" for i in range(10)})
    response = await client.post("/api/files/", files=upload_multipart("many.zip", many))
    assert response.status_code == 400
    assert "too many entries" in response.json()["detail"]


# ------------------------------------------------------------ unit: utilities

def test_safe_extract_rejects_traversal(tmp_path):
    archive = tmp_path / "evil.zip"
    archive.write_bytes(malicious_traversal_zip())
    with pytest.raises(UnsafeArchiveError):
        safe_extract_zip(archive, tmp_path / "out", max_entries=10, max_expanded_bytes=1_000_000)
    assert not (tmp_path / "evil.shp").exists()
    assert not (tmp_path / "out" / ".." / "evil.shp").exists()


def test_safe_extract_missing_shapefile(tmp_path):
    archive = tmp_path / "data.zip"
    archive.write_bytes(zip_with_files({"a.txt": b"hi"}))
    with pytest.raises(MissingShapefileError):
        safe_extract_zip(archive, tmp_path / "out", max_entries=10, max_expanded_bytes=1_000_000)


def test_safe_extract_invalid_zip(tmp_path):
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"not really a zip")
    with pytest.raises(InvalidArchiveError):
        safe_extract_zip(archive, tmp_path / "out", max_entries=10, max_expanded_bytes=1_000_000)


@pytest.mark.parametrize(
    ("raw", "expected_suffix"),
    [
        ("../../etc/passwd.kml", ".kml"),
        ("..\\..\\windows\\evil.kml", ".kml"),
        ("/absolute/path/survey.kml", ".kml"),
        ("normal.kml", ".kml"),
        ("", ".kml"),  # falls back to 'upload' → no extension, still not traversal
    ],
)
def test_sanitize_filename_never_keeps_directories(raw, expected_suffix):
    result = sanitize_filename(raw)
    assert "/" not in result
    assert "\\" not in result
    assert ".." not in result
    if raw:
        assert result.endswith(expected_suffix) or result == "upload"


def test_sanitize_filename_strips_hidden_and_control_chars():
    assert sanitize_filename(".hidden.kml") == "hidden.kml"
    assert sanitize_filename("weird\x00name\u202ekml") == "weird_name_kml"
    assert sanitize_filename(None) == "upload"
    assert sanitize_filename("..") == "upload"


async def test_upload_happy_path_still_works_after_security_layers(client):
    """Security checks must not break legitimate uploads."""
    response = await client.post(
        "/api/files/", files=upload_multipart("survey.kml", kml_bytes())
    )
    assert response.status_code == 201
    assert response.json()["status"] == "COMPLETED"
